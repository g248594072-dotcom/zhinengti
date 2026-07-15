 聊天记录质检工具

对销售聊天记录进行 AI 质检，按客户发言数分级处理（跳过 / 轻量 / 完整），输出 Excel 报告。

**安全提醒：切勿将 `qc_config.json` 或真实 API Key 提交到 Git、发给他人或打进安装包。仅在本机保留配置文件。**

## 项目用途

- 读取聊天记录 Excel（会话 ID、联系人、消息内容等列）
- 按客户发言条数自动分档：少于 4 条跳过、4~6 条轻量质检、大于 6 条完整质检
- 调用 DeepSeek 等 OpenAI 兼容接口，按 `prompts/` 中的标准做结构化质检判断（不打分）
- 导出 Excel 质检报告（结果标签、客户阶段、是否合格、风险等级、红线、问题与建议、下一步跟进动作）

提供两种界面：

| 版本 | 文件 | 适合 |
|------|------|------|
| 桌面版 | `智能体/聊天质检工具.py` | 本机选文件、保存报告到磁盘 |
| 网页版 | `智能体/streamlit_app.py` | 浏览器上传、看进度、下载报告 |

核心逻辑统一在 `智能体/qc_core.py`。

## 目录结构

```
工具库/
├── 智能体/
│   ├── 聊天质检工具.py       # 桌面版（Tkinter）
│   ├── streamlit_app.py      # 网页版（Streamlit）
│   ├── qc_core.py            # 共用核心逻辑
│   ├── qc_config.example.json  # 配置模板（可提交 Git）
│   ├── qc_config.json        # 本地配置（勿提交，需自行创建）
│   ├── prompts/
│   │   ├── full_qc.md        # 完整质检标准
│   │   └── lite_qc.md        # 轻量质检标准
│   └── requirements.txt
├── tests/
│   └── test_qc_core.py       # 单元测试
├── 聊天记录/                 # 原始聊天 Excel
├── MD文档/                     # 标准、话术、培训文档
└── 原始文档/                   # Word / XMind 源文件
```

## 安装依赖

```powershell
cd d:\0221\工具库\智能体
pip install -r requirements.txt
```

## 创建 qc_config.json（首次必做）

1. 进入 `智能体` 文件夹
2. 复制模板：

```powershell
copy qc_config.example.json qc_config.json
```

3. 编辑 `qc_config.json`，将 `api_key` 改为你的真实密钥：

```json
{
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-pro",
  "api_key": "YOUR_API_KEY_HERE",
  "concurrency": 8,
  "max_chars": 16000,
  "app_password": ""
}
```

将 `YOUR_API_KEY_HERE` 替换为 DeepSeek 控制台创建的 Key。**不要把填好 Key 的文件提交到 Git。**

- **桌面版**：可读取并保存 `qc_config.json`
- **网页版**：优先读 `qc_config.json`；若不存在则读 `.streamlit/secrets.toml`（见 `secrets.toml.example`）；使用者**无法在网页填写 API Key**
- **app_password**（可选）：网页访问密码，与 API Key 无关；留空则无需密码

---

## 运行桌面版

```powershell
cd d:\0221\工具库\智能体
python 聊天质检工具.py
```

1. 确认或填写 API 配置，点「保存配置」
2. 点「测试连接」
3. 选择 xlsx 表格开始质检
4. 报告保存到所选文件同目录

### 打包 exe（可选）

```powershell
pip install pyinstaller
pyinstaller -F -w --name 聊天质检工具 --add-data "prompts;prompts" 聊天质检工具.py
```

分发时将 `聊天质检工具.exe` 与 `prompts` 文件夹放在同一目录；**不要**把含真实 Key 的 `qc_config.json` 打包给别人。

---

## 运行 Streamlit 网页版

```powershell
cd d:\0221\工具库\智能体
streamlit run streamlit_app.py
```

浏览器访问 `http://localhost:8501`（通常会自动打开）。

1. 确认顶部「已加载配置」（否则按提示创建 `qc_config.json` 后刷新）
2. 上传 `.xlsx` → 确认列映射 → 开始质检 → 下载报告

---

## 修改质检标准（Prompt）

编辑以下文件后**重启程序**（同一次运行内有缓存，不会热更新）：

- `智能体/prompts/full_qc.md` — 完整质检（客户发言 > 6 条）
- `智能体/prompts/lite_qc.md` — 轻量质检（4~6 条）

桌面版与网页版共用同一套 prompt 文件。

---

## 运行测试

在项目根目录执行：

```powershell
cd d:\0221\工具库
python -m unittest discover -s tests -v
```

测试覆盖：发言数统计（带/不带时间戳）、系统消息不计入客服、分级边界（skip / light / full）。**不调用 API。**

---

## 分级规则

| 档位 | 条件（客户发言条数） | 说明 |
|------|----------------------|------|
| 跳过 | < 4 | 不调 API，仅记录发言数 |
| 轻量 | 4 ~ 6 | 短 prompt 快速质检 |
| 完整 | > 6 | 完整结构化质检（不打分） |

## 高风险判定

命中红线、判定「不合格」、风险等级为「高」、需人工复核、或单通 API/JSON 解析失败，均计入高风险数量。

## 更多说明

详见 [`智能体/质检工具_使用说明.md`](智能体/质检工具_使用说明.md)。
