# TapTap 游戏批量更新

让大模型操作手机上的 TapTap，把「我的游戏 → 本机 → N 款游戏可更新」里的游戏**全部更新并装完**。
和活动采集完全独立，可以单独做成每天的定时任务（例如凌晨先更新，白天再采集）。

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

## 需要注意

- `prompts/system_prompt.txt` 第 6 条标了 **【测试阶段设置】**：TapTap 下载需要的普通权限弹窗（照片 / 视频 / 音频 / 存储 / 通知）由模型点「允许」。上线前确认是否保留。
  「是否允许 TapTap 安装应用」始终不代点。
- 安装器页面里的推荐应用「安装」按钮、「开启安全守护 / 增强防护」都在 prompt 里明确禁止；TapTap 自身的更新弹窗一律关闭。
- 多款游戏的安装冲突（一个安装被另一个打断）已写进 prompt，但尚未在真机上遇到过。

## 实测（2026-09-14，红米 Note 12T Pro，qwen3.8-flash）

| 场景 | 结果 |
|---|---|
| 单款：明日方舟 | 2.7.61 → 2.7.71，15 步约 4 分钟 |
| 全部更新：三国：谋定天下 + 无畏契约：源能行动 | 1.31.0 → 1.36.0、1.23.0 → 1.24.0，TapTap 保持 2.96.4；22 步约 7 分钟；途中关掉了 TapTap 自身的更新弹窗 |
| 没有待更新的游戏 | 2 步识别「暂无可更新的游戏」后结束（用 autoglm `main` 代码跑） |
