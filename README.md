# TapTap 游戏批量更新

和活动采集完全独立的两个维护任务，可以单独做成定时任务（例如凌晨先更新、再检测登录，白天再采集）：

1. **TapTap 批量更新**：把「我的游戏 → 本机 → N 款游戏可更新」里的游戏全部更新并装完。
2. **游戏登录检测**：逐个启动游戏，确认能进到游戏里；需要登录的用 QQ / 微信一键授权登录。

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
| `update_games.py` | 运行入口 |
| `prompts/system_prompt.txt` | 系统提示：动作格式、坐标规则、MIUI 安装器每个弹窗的处理、不更新 TapTap 自身、权限弹窗、完成判定 |
| `prompts/task_update_all.txt` | 任务：全部更新的步骤 |
| `prompts/system_login_check.txt` | 登录检测的规则（系统提示） |
| `prompts/task_login_check.txt` | 登录检测任务，`{game}` 由运行时的游戏名替换 |
| `prompts/task_login_check_heping.txt`、`_wangzhe.txt` | 填好游戏名的测试版，内容与模板一致 |
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

线上采集每次都会先结束游戏再冷启动，所以游戏是否还处于登录态要先确认，否则采集会卡在登录页。

```
启动游戏 → 等加载
  → 判断三种情况：已登录 / 需要登录 / 还在加载
  → 需要登录：优先 QQ，其次微信，按屏幕上实际出现的按钮一步步授权
  → 确认真的进入游戏后结束
```

要点：

- **登录步骤不写死**：不按固定步数点。规则是按按钮语义走 —— 「继续 / 授权 / 登录 / 同意 / 确认 / 允许 / 下一步」可以点，
  「取消 / 拒绝 / 切换账号 / 更换登录方式 / 注册 / 退出登录」不点。实测两个游戏的授权页数、按钮文案都不一样，都能走通。
- **登录页可能只是加载过场**：有些游戏加载时会闪过登录页，等一会儿自己就进去了。所以第一次看到登录页先等 15–20 秒再判断，
  仍停在登录页才去点。
- **判定只看一条**：是否真的进入了游戏（大厅 / 主界面、「开始游戏」按钮、或选区页）。登录按钮消失、弹窗关掉都不算。
  进了大厅就立即结束，不去逐个关活动弹窗。
- **不碰的事**：账号密码、手机号、验证码、扫码、实名认证、人脸识别、充值、开始对局。遇到就结束并返回「需要人工：…」。

运行（目前借 autoglm 的入口跑，需要该入口支持 `--prompt-file` / `--system-prompt-file`；接进 `update_games.py` 是下一步）：

```bash
cd /path/to/autoglm && python run.py --device <设备ID> --game "和平精英" \
  --prompt-file /path/to/taptap-game-updater/prompts/task_login_check.txt \
  --system-prompt-file /path/to/taptap-game-updater/prompts/system_login_check.txt \
  --max-steps 60 --run-label login-check
```

一次检测一个游戏，跑完就结束；多个游戏由外层脚本按手机的游戏清单逐个跑，互不影响。

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



## 实测（2026-09-14 更新、09-19 登录检测；红米 Note 12T Pro，qwen3.8-flash）

| 场景 | 结果 |
|---|---|
| 单款：明日方舟 | 2.7.61 → 2.7.71，15 步约 4 分钟 |
| 全部更新：三国：谋定天下 + 无畏契约：源能行动 | 1.31.0 → 1.36.0、1.23.0 → 1.24.0，TapTap 保持 2.96.4；22 步约 7 分钟；途中关掉了 TapTap 自身的更新弹窗 |
| 没有待更新的游戏 | 2 步识别「暂无可更新的游戏」后结束（用 autoglm `main` 代码跑） |
| 登录检测：和平精英（需登录） | 走完 QQ 三页授权进入大厅，14 步约 2 分钟 |
| 登录检测：王者荣耀（需登录） | QQ 两页授权，中间退回登录页自行重试一次，9 步 |
| 登录检测：王者荣耀（已登录） | 3 步判定「无需登录，已进入游戏」 |
| 登录检测：和平精英（登录页为加载过场） | 先等待不点，5 步判定「无需登录」，全程未点登录按钮 |
