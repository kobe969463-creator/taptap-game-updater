"""TapTap 游戏批量更新：让大模型在 TapTap 里点「全部更新」并装完，独立于活动采集，可单独定时运行。

依赖 autoglm 仓库里的 phone_agent（Agent 循环、模型调用、adb 操作），本仓库只提供 prompt 和运行流程：
  点亮解锁 → 记录更新前各应用版本 → 模型在 TapTap 里全部更新 → 回桌面 → 对比版本，输出更新了哪些。

用法：
  python update_games.py                         # 更新所有已连接的手机
  python update_games.py --device <设备ID>
  python update_games.py --daily 03:00           # 每天 03:00 跑一次
  python update_games.py --dry-run               # 只打印配置和手机列表，不调用模型
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAPTAP_PACKAGE = "com.taptap"
NEED_HUMAN_PREFIX = "需要人工"
# autoglm 非正常结束时的结束信息：步数用满、画面长时间不变、连续解析失败 / 空动作
FAILURE_MARKERS = ("Max steps reached", "画面无变化", "强制结束")


# ---------------------------------------------------------------- 配置

def load_env(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 格式的 .env（忽略注释和空行，去掉值两侧引号）。"""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


_AUTOGLM: dict | None = None


def import_autoglm(path: str):
    """把 autoglm 仓库加入 import 路径，返回需要用到的模块（只导入一次）。"""
    global _AUTOGLM
    if _AUTOGLM is not None:
        return _AUTOGLM
    root = Path(path).expanduser().resolve()
    if not (root / "phone_agent" / "agent.py").is_file():
        sys.exit(f"找不到 autoglm 仓库：{root}（用 --autoglm 或 .env 的 AUTOGLM_PATH 指定）")
    sys.path.insert(0, str(root))
    from phone_agent.agent import AgentConfig, PhoneAgent
    from phone_agent.config import apps
    from phone_agent.device_factory import get_device_factory
    from phone_agent.model import ModelConfig

    # 模型用 do(action="Launch", app="TapTap") 打开 TapTap，需要这条包名映射
    apps.APP_PACKAGES.setdefault("TapTap", TAPTAP_PACKAGE)
    try:  # 多台手机并行时让日志逐行带设备标签（autoglm 里有就用）
        from phone_agent._print import install_thread_safe_print, set_device_label
        install_thread_safe_print()
    except ImportError:
        set_device_label = None
    _AUTOGLM = dict(PhoneAgent=PhoneAgent, AgentConfig=AgentConfig, ModelConfig=ModelConfig,
                    get_device_factory=get_device_factory, apps=apps, set_device_label=set_device_label)
    return _AUTOGLM


def build_model_config(ModelConfig, env: dict[str, str]):
    extra_body = env.get("EXTRA_BODY", "").strip()
    values = dict(
        base_url=env.get("BASE_URL", ""),
        api_key=env.get("API_KEY", ""),
        model_name=env.get("MODEL_NAME", ""),
        max_tokens=int(env.get("MAX_TOKENS", 3000)),
        temperature=float(env.get("TEMPERATURE", 0)),
        top_p=float(env.get("TOP_P", 0.85)),
        frequency_penalty=float(env.get("FREQUENCY_PENALTY", 0)),
        extra_body=json.loads(extra_body) if extra_body else {},
        image_detail=env.get("IMAGE_DETAIL") or None,
    )
    # 不同版本的 autoglm ModelConfig 字段不完全一样（如 image_detail），只传它认识的
    known = {f.name for f in dataclasses.fields(ModelConfig)}
    return ModelConfig(**{k: v for k, v in values.items() if k in known})


# ---------------------------------------------------------------- 手机

def adb(device: str, *args: str, timeout: int = 60) -> str:
    r = subprocess.run(["adb", "-s", device, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=timeout)
    return r.stdout or ""


def connected_devices() -> list[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    return [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]


def app_versions(device: str) -> dict[str, dict[str, str]]:
    """第三方应用的 versionName / lastUpdateTime，用来判断更新前后哪些应用变了。"""
    third_party = {l.replace("package:", "").strip()
                   for l in adb(device, "shell", "pm", "list", "packages", "-3").splitlines() if l.strip()}
    dump = adb(device, "shell", "dumpsys", "package", "packages", timeout=120)
    versions: dict[str, dict[str, str]] = {}
    for block in re.split(r"\n  Package \[", dump)[1:]:
        pkg = block.split("]", 1)[0]
        if pkg not in third_party or pkg in versions:
            continue
        name = re.search(r"versionName=(\S+)", block)
        updated = re.search(r"lastUpdateTime=([\d-]+ [\d:]+)", block)
        versions[pkg] = {"version": name.group(1) if name else "",
                         "updated": updated.group(1) if updated else ""}
    return versions


def prepare_device(factory, device: str) -> None:
    """亮屏、解锁、回桌面，让模型从确定的状态开始。"""
    if factory.get_screen_state(device) == "off":
        factory.wake_up(device)
    if hasattr(factory, "set_brightness"):
        factory.set_brightness(255, device)
    if factory.is_locked(device) and not factory.dismiss_keyguard(device):
        print(f"⚠️ {device} 自动解锁失败，模型可能看到锁屏")
    factory.home(device)
    time.sleep(1.0)


# ---------------------------------------------------------------- 单台手机

def update_one_device(device: str, args, ag: dict, model_config, system_prompt: str,
                      task_prompt: str, run_dir: Path) -> dict:
    if ag["set_device_label"]:
        ag["set_device_label"](device[:8])
    factory = ag["get_device_factory"]()
    out_dir = run_dir / device
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"device": device, "started_at": datetime.now().isoformat(timespec="seconds")}

    before = app_versions(device)
    prepare_device(factory, device)

    agent = ag["PhoneAgent"](
        model_config=model_config,
        agent_config=ag["AgentConfig"](
            lang="cn", verbose=True, max_steps=args.max_steps, max_stuck_steps=args.max_stuck_steps,
            device_id=device, system_prompt=system_prompt,
        ),
        # 定时任务没人值守：模型请求人工时只记日志，不在控制台等回车
        takeover_callback=lambda msg: print(f"✋ 需要人工：{msg}"),
        memory_manager=None,  # 不注入采集任务的历史经验
        output_dir=str(out_dir),
    )
    try:
        result["finish_message"] = agent.run(task_prompt)
    except Exception as exc:  # 模型或 adb 异常：记录后继续收尾
        traceback.print_exc()
        result["finish_message"] = f"异常：{exc}"
    finally:
        # 只回桌面，不结束 TapTap 进程：它可能还在后台安装
        try:
            factory.home(device)
            if args.lock:
                factory.lock_screen(device)
        except Exception as exc:
            print(f"⚠️ {device} 收尾失败：{exc}")

    after = app_versions(device)
    name_of = getattr(ag["apps"], "get_app_name", lambda _pkg: None)
    result["updated"] = [
        {"package": pkg, "name": name_of(pkg) or "", "from": before[pkg]["version"], "to": info["version"]}
        for pkg, info in after.items()
        if pkg in before and info != before[pkg] and pkg != TAPTAP_PACKAGE
    ]
    result["newly_installed"] = sorted(set(after) - set(before))
    result["taptap_changed"] = before.get(TAPTAP_PACKAGE) != after.get(TAPTAP_PACKAGE)
    msg = result["finish_message"] or ""
    failed = msg.startswith("异常") or any(k in msg for k in FAILURE_MARKERS)
    result["status"] = "need_human" if msg.startswith(NEED_HUMAN_PREFIX) else "error" if failed else "done"
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ---------------------------------------------------------------- 一轮

def run_once(args, env: dict[str, str]) -> int:
    ag = import_autoglm(args.autoglm or env.get("AUTOGLM_PATH") or str(HERE.parent / "autoglm"))
    model_config = build_model_config(ag["ModelConfig"], env)
    system_prompt = (HERE / "prompts" / "system_prompt.txt").read_text(encoding="utf-8").strip()
    task_prompt = (HERE / "prompts" / "task_update_all.txt").read_text(encoding="utf-8").strip()
    devices = args.device or connected_devices()

    print(f"模型：{model_config.model_name} @ {model_config.base_url}  api_key：{'已配置' if model_config.api_key else '未配置'}")
    print(f"手机：{devices or '无'}  最多 {args.max_steps} 步  结束后{'锁屏' if args.lock else '不锁屏'}")
    if args.dry_run:
        return 0
    if not devices:
        print("❌ 没有已连接的手机")
        return 1
    if not model_config.api_key:
        print("❌ .env 里没有 API_KEY")
        return 1

    run_dir = HERE / "output" / datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        futures = {pool.submit(update_one_device, d, args, ag, model_config, system_prompt, task_prompt, run_dir): d
                   for d in devices}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                traceback.print_exc()
                results.append({"device": futures[fut], "status": "error", "finish_message": str(exc), "updated": []})

    print("\n========== 更新结果 ==========")
    for r in sorted(results, key=lambda r: r["device"]):
        print(f"📱 {r['device']}  [{r['status']}]  {r.get('finish_message', '')}")
        for u in r.get("updated", []):
            print(f"   ✅ {u['name'] or u['package']}：{u['from']} → {u['to']}")
        if not r.get("updated"):
            print("   （没有应用版本发生变化）")
        if r.get("taptap_changed"):
            print("   ⚠️ TapTap 自身版本发生了变化")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果目录：{run_dir}")
    return 0 if all(r["status"] == "done" for r in results) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="TapTap 游戏批量更新（大模型驱动）")
    ap.add_argument("--device", action="append", help="只更新指定手机，可重复传；默认所有已连接的手机")
    ap.add_argument("--autoglm", help="autoglm 仓库路径；默认读 .env 的 AUTOGLM_PATH，再默认 ../autoglm")
    ap.add_argument("--env", default=str(HERE / ".env"), help="模型配置文件，默认本目录 .env")
    ap.add_argument("--max-steps", type=int, default=100)
    ap.add_argument("--max-stuck-steps", type=int, default=10, help="连续多少步画面不变判定卡住")
    ap.add_argument("--no-lock", dest="lock", action="store_false", help="结束后不锁屏（默认锁屏）")
    ap.add_argument("--daily", metavar="HH:MM", help="常驻运行，每天到点跑一次")
    ap.add_argument("--dry-run", action="store_true", help="只打印配置和手机列表")
    args = ap.parse_args()
    def current_env() -> dict[str, str]:
        """每轮重新读 .env（改了不用重启）；同名环境变量优先。"""
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
