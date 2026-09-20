"""游戏登录检测：逐个启动游戏，确认能不能真正进到游戏里；需要登录的用 QQ / 微信一键授权登录。

建议排在 TapTap 更新之后、活动采集之前：采集每次都会冷启动游戏，登录态掉了采集会卡在登录页。

判定用两道：模型自己的结论 + adb 确认结束时前台确实是这个游戏，两者都过才算进入成功。
提示词在 login-check/，依赖 autoglm 的 phone_agent（与 update_games.py 相同）。

用法：
  python login_check.py                          # 所有已连接手机，游戏清单从 autoglm 配置读
  python login_check.py --device <设备ID> --game "和平精英" --game "王者荣耀"
  python login_check.py --daily 04:00            # 每天 04:00 跑一轮
  python login_check.py --dry-run                # 只打印手机和待检测的游戏清单
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from update_games import (HERE, adb, build_model_config, connected_devices, import_autoglm,
                          load_env, prepare_device)

PROMPT_DIR = HERE / "login-check"
NEED_HUMAN_PREFIX = "需要人工"

STATUS_TEXT = {
    "already_logged_in": "无需登录",
    "logged_in": "已完成登录",
    "need_human": "需要人工",
    "failed": "进不去",
    "not_installed": "未安装",
    "no_package": "包名未配置",
}


def current_package(device: str) -> str:
    """当前前台应用的包名，用来客观确认是不是真的停在游戏里。"""
    for line in adb(device, "shell", "dumpsys", "window").splitlines():
        if "mCurrentFocus" in line and "/" in line:
            return line.split()[-1].split("/")[0].strip("}")
    return ""


def games_for_device(device: str, ag: dict, autoglm_root: Path) -> list[str]:
    """游戏清单：autoglm 的 run_config.json（设备 → phone 编号）+ prompts/phoneN.py 的 games。"""
    cfg_path = autoglm_root / "run_config.json"
    if not cfg_path.is_file():
        print(f"⚠️ {device} 找不到 {cfg_path}，请用 --game 指定游戏")
        return []
    entries = json.loads(cfg_path.read_text(encoding="utf-8")).get("devices", [])
    phone_no = next((e.get("phone") for e in entries if e.get("device_id") == device), None)
    if phone_no is None:
        print(f"⚠️ {device} 不在 run_config.json 的 devices 里，请用 --game 指定游戏")
        return []
    try:
        from prompts import PHONES
    except ImportError as exc:
        print(f"⚠️ 读取游戏清单失败：{exc}")
        return []
    return list(PHONES.get(phone_no, {}).get("games", []))


def installed_packages(device: str) -> set[str]:
    return {l.replace("package:", "").strip()
            for l in adb(device, "shell", "pm", "list", "packages").splitlines() if l.strip()}


def check_one_game(device: str, game: str, args, ag: dict, model_config, system_prompt: str,
                   task_template: str, installed: set[str], out_dir: Path) -> dict:
    factory = ag["get_device_factory"]()
    package = ag["apps"].get_package_name(game)
    result = {"game": game, "package": package or "", "started_at": datetime.now().isoformat(timespec="seconds")}

    if not package:
        result["status"] = "no_package"
        return result
    if package not in installed:
        result["status"] = "not_installed"
        return result

    factory.force_stop(package, device)  # 冷启动，才能真实测到登录态
    factory.home(device)
    time.sleep(1.0)

    agent = ag["PhoneAgent"](
        model_config=model_config,
        agent_config=ag["AgentConfig"](
            lang="cn", verbose=True, max_steps=args.max_steps, max_stuck_steps=args.max_stuck_steps,
            device_id=device, system_prompt=system_prompt,
        ),
        takeover_callback=lambda msg: print(f"✋ 需要人工：{msg}"),
        memory_manager=None,
        output_dir=str(out_dir / game),
    )
    try:
        message = agent.run(task_template.replace("{game}", game))
    except Exception as exc:
        traceback.print_exc()
        message = f"异常：{exc}"

    in_game = current_package(device) == package  # 客观校验：结束时前台确实是这个游戏
    result.update(finish_message=message, foreground_in_game=in_game)
    if message.startswith(NEED_HUMAN_PREFIX):
        result["status"] = "need_human"
    elif in_game and "无需登录" in message:
        result["status"] = "already_logged_in"
    elif in_game:
        result["status"] = "logged_in"
    else:
        result["status"] = "failed"
        if not in_game and not message.startswith("异常"):
            result["note"] = "模型称已进入，但结束时前台不是该游戏"

    factory.force_stop(package, device)
    factory.home(device)
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return result


def check_one_device(device: str, args, ag: dict, model_config, system_prompt: str,
                     task_template: str, autoglm_root: Path, run_dir: Path) -> dict:
    if ag["set_device_label"]:
        ag["set_device_label"](device[:8])
    out_dir = run_dir / device
    out_dir.mkdir(parents=True, exist_ok=True)

    games = args.game or games_for_device(device, ag, autoglm_root)
    installed = installed_packages(device)
    prepare_device(ag["get_device_factory"](), device)

    results = []
    for game in games:
        print(f"\n{'#' * 40}\n📱 {device} → 检测《{game}》\n{'#' * 40}")
        results.append(check_one_game(device, game, args, ag, model_config, system_prompt,
                                      task_template, installed, out_dir))
        (out_dir / "result.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.lock:
        try:
            ag["get_device_factory"]().lock_screen(device)
        except Exception as exc:
            print(f"⚠️ {device} 锁屏失败：{exc}")
    return {"device": device, "games": results}


def run_once(args, env: dict[str, str]) -> int:
    autoglm_root = Path(args.autoglm or env.get("AUTOGLM_PATH") or (HERE.parent / "autoglm")).expanduser().resolve()
    ag = import_autoglm(str(autoglm_root))
    model_config = build_model_config(ag["ModelConfig"], env)
    system_prompt = (PROMPT_DIR / "system_prompt.txt").read_text(encoding="utf-8").strip()
    task_template = (PROMPT_DIR / "task.txt").read_text(encoding="utf-8").strip()
    devices = args.device or connected_devices()

    print(f"模型：{model_config.model_name} @ {model_config.base_url}  api_key：{'已配置' if model_config.api_key else '未配置'}")
    print(f"手机：{devices or '无'}  每个游戏最多 {args.max_steps} 步  结束后{'锁屏' if args.lock else '不锁屏'}")
    for d in devices:
        print(f"  {d} 待检测：{args.game or games_for_device(d, ag, autoglm_root) or '（清单为空）'}")
    if args.dry_run:
        return 0
    if not devices:
        print("❌ 没有已连接的手机")
        return 1
    if not model_config.api_key:
        print("❌ .env 里没有 API_KEY")
        return 1

    run_dir = HERE / "output" / "login-check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    devices_result = []
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = {pool.submit(check_one_device, d, args, ag, model_config, system_prompt,
                               task_template, autoglm_root, run_dir): d for d in devices}
        for fut in as_completed(futures):
            try:
                devices_result.append(fut.result())
            except Exception as exc:
                traceback.print_exc()
                devices_result.append({"device": futures[fut], "games": [], "error": str(exc)})

    print("\n========== 登录检测结果 ==========")
    ok = True
    for dev in sorted(devices_result, key=lambda d: d["device"]):
        print(f"📱 {dev['device']}")
        for g in dev["games"]:
            status = STATUS_TEXT.get(g["status"], g["status"])
            mark = "✅" if g["status"] in ("already_logged_in", "logged_in") else "⚠️"
            ok = ok and g["status"] in ("already_logged_in", "logged_in")
            print(f"   {mark} {g['game']}：{status}" + (f"  —— {g.get('finish_message', '')}" if mark == "⚠️" else ""))
        if not dev["games"]:
            print("   （没有待检测的游戏）")
            ok = False
    (run_dir / "summary.json").write_text(
        json.dumps(devices_result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{run_dir}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="游戏登录检测（大模型驱动）")
    ap.add_argument("--device", action="append", help="只检测指定手机，可重复传；默认所有已连接的手机")
    ap.add_argument("--game", action="append", help="只检测指定游戏，可重复传；默认读 autoglm 的游戏清单")
    ap.add_argument("--autoglm", help="autoglm 仓库路径；默认读 .env 的 AUTOGLM_PATH，再默认 ../autoglm")
    ap.add_argument("--env", default=str(HERE / ".env"), help="模型配置文件，默认本目录 .env")
    ap.add_argument("--max-steps", type=int, default=60, help="单个游戏最多多少步，默认 60")
    ap.add_argument("--max-stuck-steps", type=int, default=10, help="连续多少步画面不变判定卡住")
    ap.add_argument("--no-lock", dest="lock", action="store_false", help="结束后不锁屏（默认锁屏）")
    ap.add_argument("--daily", metavar="HH:MM", help="常驻运行，每天到点跑一次")
    ap.add_argument("--dry-run", action="store_true", help="只打印手机和待检测的游戏清单")
    args = ap.parse_args()

    def current_env() -> dict[str, str]:
        overrides = {k: v for k, v in os.environ.items()
                     if k in ("API_KEY", "BASE_URL", "MODEL_NAME", "EXTRA_BODY", "AUTOGLM_PATH")}
        return {**load_env(Path(args.env)), **overrides}

    if not args.daily:
        sys.exit(run_once(args, current_env()))

    hour, minute = (int(x) for x in args.daily.split(":"))
    print(f"⏰ 每天 {hour:02d}:{minute:02d} 运行，Ctrl+C 退出")
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        print(f"下一次：{nxt:%Y-%m-%d %H:%M}")
        time.sleep((nxt - now).total_seconds())
        try:
            run_once(args, current_env())
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
