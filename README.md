# TapTap 游戏批量更新

和活动采集完全独立的两个维护任务，可以单独做成定时任务（例如凌晨先更新、再检测登录，白天再采集）：

1. **TapTap 批量更新**：把「我的游戏 → 本机 → N 款游戏可更新」里的游戏全部更新并装完。
2. **游戏登录检测**：逐个启动游戏，确认能进到游戏里；需要登录的用 QQ / 微信一键授权登录。

## 本次更新（2026-09-19）：新增游戏登录检测

TapTap 批量更新部分此前已交付并合并，本次**没有改动**；这一轮新增的内容全部在 [`login-check/`](login-check/) 一个文件夹里，单独查看即可：

| 新增 | 说明 |
|---|---|
| `login-check/system_prompt.txt` | 登录检测的规则（系统提示） |
| `login-check/task.txt` | 登录检测任务，`{game}` 由运行时的游戏名替换 |
| `login-check/examples/` | 填好「和平精英」「王者荣耀」的版本，测试时直接用 |
| `login-check/README.md` | 用法、设计要点、实测记录 |
| `login_check.py` | 登录检测的运行入口：按游戏清单逐个检测、汇总结果、可定时跑（**尚未实跑验证**） |

一句话说明它解决什么：线上采集每次都会先结束游戏再冷启动，登录态掉了采集就会卡在登录页。
这个任务在采集之前逐个启动游戏，确认能真正进到游戏里，需要登录的自己用 QQ / 微信一键授权登录完再确认一次。

登录步骤**没有写死**：不按固定步数点，按按钮语义走（继续 / 同意 / 确认 / 授权就点，取消 / 拒绝 / 切换账号不点），
所以不同游戏的流程差异能自己应付。实测和平精英与王者荣耀的登录入口文案、授权页数完全不同，都走通了。
详见 [`login-check/README.md`](login-check/README.md)。

## 工作方式

```
点亮、解锁、回桌面
  → 记录更新前所有第三方应用的版本号
  → 大模型：打开 TapTap → 我的游戏 → 本机 →「N 款游戏可更新」→「全部更新」
            → 等下载 → 逐个处理系统安装界面 → 确认更新列表为空
  → 回桌面、锁屏（不结束 TapTap 进程，避免打断后台安装）
  → 再次读取版本号，对比出「哪些游戏从什么版本更新到什么版本」
```

- 要更新哪些游戏**不写在 prompt 里**，由模型从 TapTap 屏幕上自己判断。
- 是否装好以 adb 读到的版本号为准，不只看模型自己说「完成」。
- 多台手机同时跑，每台一个独立的 Agent。

## 目录

| 文件 | 作用 |
|---|---|
| `update_games.py` | TapTap 批量更新的运行入口 |
| `login_check.py` | 登录检测的运行入口（尚未实跑验证） |
| `prompts/system_prompt.txt` | 系统提示：动作格式、坐标规则、MIUI 安装器每个弹窗的处理、不更新 TapTap 自身、权限弹窗、完成判定 |
| `prompts/task_update_all.txt` | 任务：全部更新的步骤 |
| `login-check/` | 登录检测：规则、任务、示例和说明（见该目录下的 README） |
| `.env.example` | 模型配置模板 |

## 依赖

本仓库**不包含** Agent 代码，运行时从 autoglm 仓库导入 `phone_agent`（模型调用、截图、adb 点击）。

1. 本机有一份 autoglm 仓库，并能在它的 Python 环境里运行（`openai`、`Pillow` 等依赖由 autoglm 提供）。
2. 用 autoglm 的 Python 运行本脚本，例如：
   ```bash
   cp .env.example .env        # 填 API_KEY；AUTOGLM_PATH 指向 autoglm 仓库
   /path/to/autoglm/.venv/bin/python update_games.py --dry-run
   ```

已在 autoglm `main`（ea95ef8）上验证可用。autoglm 若合入了「坐标越界拦截 / 失败原因反馈」修复，本脚本会自动受益，不需要改动。

## 运行

```bash
python update_games.py                          # 所有已连接的手机
python update_games.py --device <设备ID>         # 指定手机，可重复传 --device
python update_games.py --daily 03:00            # 常驻，每天 03:00 跑一轮
python update_games.py --no-lock                # 结束后不锁屏
python update_games.py --max-steps 150          # 更新的游戏多、下载慢时放宽步数（默认 100）
```

也可以交给系统的定时任务，例如 crontab：

```cron
0 3 * * * cd /path/to/taptap-game-updater && /path/to/autoglm/.venv/bin/python update_games.py >> output/cron.log 2>&1
```

## 登录检测（建议排在更新之后）

完整说明见 [`login-check/README.md`](login-check/README.md)。

```bash
python login_check.py                          # 所有已连接手机，游戏清单从 autoglm 配置读
python login_check.py --device <设备ID> --game "和平精英" --game "王者荣耀"
python login_check.py --daily 04:00            # 每天 04:00 跑一轮
python login_check.py --dry-run                # 只打印手机和待检测的游戏清单
```

- 游戏清单来自 autoglm 的 `run_config.json`（设备 → phone 编号）和 `prompts/phoneN.py` 的 `games`，也可用 `--game` 指定。
- 每个游戏先结束进程冷启动，跑完再结束；一个游戏卡住不影响后面的。
- 判定两道：模型的结论 + adb 确认结束时前台确实是该游戏。模型说进去了但前台不对，会单独标出来。
- 输出四种状态：无需登录 / 已完成登录 / 需要人工 / 进不去。全部通过时退出码为 0。

> ⚠️ **`login_check.py` 本身还没有实跑验证过**（写完时测试手机已拔线），只做了语法检查和 `--dry-run`。
> 提示词部分是真机测过的（见 `login-check/README.md` 的实测表）。第一次用建议先 `--dry-run`，
> 再 `--game` 指定一两个游戏小范围试，确认无误再交给定时任务。

## 输出

`output/<时间>/summary.json`：每台手机一条，另外每台手机有自己的 `result.json`：

```json
{
  "device": "<设备ID>",
  "status": "done",
  "finish_message": "已更新全部 2 款游戏：……",
  "updated": [
    {"package": "com.bilibili.nslg", "name": "", "from": "1.31.0", "to": "1.36.0"}
  ],
  "taptap_changed": false
}
```

`status`：`done` 正常结束；`need_human` 需要人工（如授予 TapTap 安装权限）；`error` 步数用满、长时间卡住或异常。
退出码：所有手机都是 `done` 时为 0，否则为 1。

## 手机侧前置条件（每台手机做一次，需人工）

| 项 | 位置 | 不做的后果 |
|---|---|---|
| USB 调试（安全设置） | 设置 → 更多设置 → 开发者选项（小米需插 SIM 卡并登录小米账号） | 模型的点击全部无效 |
| 允许 TapTap 安装应用 | 第一次用 TapTap 安装时弹出，勾「记住我的选择」 | 模型会结束并返回 `need_human` |
| TapTap 的照片 / 视频 / 音频权限 | 第一次下载时弹出，或在系统设置里给 TapTap 授权 | 目前 prompt 设置为由模型点「允许」（见下） |



## 实测：TapTap 更新（2026-09-14，红米 Note 12T Pro，qwen3.8-flash）

| 场景 | 结果 |
|---|---|
| 单款：明日方舟 | 2.7.61 → 2.7.71，15 步约 4 分钟 |
| 全部更新：三国：谋定天下 + 无畏契约：源能行动 | 1.31.0 → 1.36.0、1.23.0 → 1.24.0，TapTap 保持 2.96.4；22 步约 7 分钟；途中关掉了 TapTap 自身的更新弹窗 |
| 没有待更新的游戏 | 2 步识别「暂无可更新的游戏」后结束（用 autoglm `main` 代码跑） |
