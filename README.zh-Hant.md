<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/wordmark-dark.svg">
    <img src="assets/logo/wordmark.svg" alt="brunna" width="420">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/imaknas/brunna/actions/workflows/ci.yml"><img src="https://github.com/imaknas/brunna/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/imaknas/brunna/releases"><img src="https://img.shields.io/github/v/release/imaknas/brunna" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
</p>

<p align="center"><a href="README.md">English</a> · 繁體中文</p>

**很多專案，交給 AI agent 去跑。每個專案都記得自己的事，由你來決定。**

brunna 在你自己的電腦上，墊在你的 AI coding agent(Claude Code、Codex 等)底下。你的 agent 會把做了什麼、學到
什麼記下來;每天晚上 brunna 把這些筆記整理進每個專案的目前狀態，再把所有專案裡重要的事收在一起，換了 session
或換了工具，東西都不會掉。只有你能決定的事，會整理成一份清單回到你手上。

brunna 在 v0.7 以前叫 agent-hub。之前裝的會保留原本的資料夾,兩個指令名稱都能用;見
[docs/upgrading.md](docs/upgrading.md#from-agent-hub)。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/loop.zh-Hant-dark.svg">
  <img src="assets/readme/loop.zh-Hant.svg" alt="agent 記下工作，每晚由 brunna 整理，只有你能決定的事回到你手上，你的回答讓工作接著做下去" width="100%">
</picture>

實際畫面:你自己電腦上的狀態頁。各專案要你回答的問題、要你做的事，新的在前;agent 之間互寄、還沒結的信;
每晚的工作有沒有跑(專案是虛構的)。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/status-page.zh-Hant-dark.png">
  <img src="assets/readme/status-page.zh-Hant.png" alt="brunna 的狀態頁:4 件等你處理、3 封信未結、0 個問題、排程器正常;幾個專案要你回答的問題和待辦，新的在前，以及各專案未結的信" width="100%">
</picture>

每週一，還有這週怎麼過的回顧:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/weekly-review.zh-Hant-dark.png">
  <img src="assets/readme/weekly-review.zh-Hant.png" alt="brunna 的每週回顧:一句這週的標題、這週的數字、看到的模式(各附依據和建議)，以及一週每天每小時的工作分布" width="100%">
</picture>

## 安裝

**會用終端機**(macOS 或 Linux):貼這一行，裝好後它會問你幾個問題，第一題是語言:

```bash
curl -fsSL https://raw.githubusercontent.com/imaknas/brunna/main/install.sh | sh
```

**不用終端機**:把下面這段貼進 Claude Desktop 的 Code 分頁或 Codex App,在對話裡回答它
([過程怎麼進行](#交給你的-ai-app-安裝)):

> 幫我從 https://github.com/imaknas/brunna 安裝 brunna。照它 README 裡「Set up through your agent」的步驟做,
> 每個問題都在這個對話裡問我，要安裝任何東西之前先告訴我。

手動安裝、需求、會睡眠的電腦、檔案放在哪裡:[docs/install.md](docs/install.md)(英文)。

## 開始用

1. 接上你已經有的資料夾。brunna 會判斷每個資料夾是一個專案、一個 monorepo,還是一個放了好幾個 repo 的資料夾，
   再問你:

   ```bash
   brunna connect ~/code
   ```

2. 在專案資料夾裡打開你的 agent(`claude` 或 `codex`;brunna 用
   [nvm 的 node](docs/install.md#requirements) 跑 Claude Code)。session 開始時，Claude Code 會載入這個專案的設定，邊做事邊寫筆記;Codex 目前透過 brunna 的工具
   (MCP)用信箱和筆記。
3. `brunna doctor` 會列出還缺什麼，每項都附上怎麼補。狀態頁(網址用 `brunna glance` 看)讓你不開終端機也能看到
   一切是否正常([從別台電腦看](docs/install.md#the-status-page-from-another-computer)、
   [在容器裡](docs/install.md#without-launchd-or-systemd)沒有東西替你開這個網址)。

隔天早上，摘要和待辦清單已經在等你。它們是模型寫的:沒有模型，晚上什麼都不會發生(沒有摘要、沒有 `todo.md`，
每個 `STATE.md` 都停在佔位的樣子)，直到你跑 `brunna llm setup`。

```markdown
# 跨專案待辦

## 只有 Alex 能做
### shop-api
- [ ] 決定退款要不要第二個人核准
## agent 可以做
### thesis
- [ ] 用修正過的資料集重跑第三章的圖
```

## 它做什麼

**記得**,支援 MCP 的 agent 都行,不需要模型。**自己會跑**,要一個模型,目前大多要 Claude Code。

- **記得自己的專案。** 筆記放在每個專案旁邊，跟程式碼分開;每個專案都有一份目前狀態，事情變了就跟著修正。
- **早上一眼看完。** 所有專案發生了什麼的摘要，加上一份待辦：只有你能做的、要先決定的、agent 可以接手的。每週一還有一份這週怎麼過的回顧。
- **你來決定。** 只有你能拍板的事會交給你(或透過開放的 [Principal Handoff](https://doi.org/10.5281/zenodo.23852711)
  協定交給你的個人助理),你回答後再送回專案。
- **專案之間會溝通。** 專案之間有信箱;沒人接的請求，平台會派 agent 去處理。
- **自己會跑。** 排程和看門狗、各專案的背景服務、警報、狀態頁。會睡眠的筆電醒來後會補跑。
- **專案可以隔開。** 開了 sandbox 的專案(`brunna connect` 會問)讀不到其他專案，也讀不到你的憑證;宣告過的憑證會被檢查、從不顯示;
  雲端備份要你自己打開，開了就有加密。
- **英文或繁體中文**,你看到的和 agent 寫的都可以選。

完整功能說明:[docs/features.md](docs/features.md)(英文)。

## 可以搭配

| 項目 | 可以用 |
|---|---|
| 你的 AI agent | 見[下面](#各個-agent-能用到什麼)。任何有 headless 模式的 CLI agent,或支援 [Agent Client Protocol](https://agentclientprotocol.com/) 的 agent,也可以和 Claude Code 搭配，在拋棄式副本裡處理信箱的請求 |
| brunna 做判斷用的模型 | 你的 agent 自己的登入、Anthropic、任何相容 OpenAI 的 API,或本機的 Ollama(`brunna llm setup`) |
| 電腦 | macOS 或 Linux。一直開著的機器最好，筆電也可以 |

還沒有 agent 或模型?筆記、排程和信箱照樣能用，每晚的整理不會跑;`brunna doctor` 會說哪些功能關著、為什麼。

### 各個 agent 能用到什麼

| 你用 | 記得、專案之間能溝通 | 自己會跑 |
|---|---|---|
| Claude Code,或 Claude Desktop 的 Code 分頁 | 有 | 有 |
| Codex | 有 | 每晚整理筆記,要自己選(`brunna llm setup`) |
| Claude Code 加 Codex | 有，共用 | 有,由 Claude Code 做 |
| Copilot、Cursor 或其他支援 MCP 的 agent | 有,要自己設定;Copilot 沒實測 | 每晚整理筆記,要自己準備模型 |

### 有沒有個人助理

只有你能決定的事,會到狀態頁和 `brunna owner`,不用設定。有個人助理的話,可以改成送到它那裡,你在手機上就能回答。
[大家怎麼用 brunna](docs/user-stories.md)(英文)有兩種情況,以及每個 agent 的用法。

## 隱私與安全

- 一切都留在你的電腦上：設定、筆記和金鑰放在 `~/.brunna`,不在 repo 裡。
- 模型呼叫只送到你選的 provider。每週回顧送的是統計數字，從不送對話內容;開了助理模組時，
  還會送你電腦上的模型寫的 session 摘要。
- brunna 沒有自己的 Claude 登入:token 由你自己產生、自己存。brunna 讀那個檔,只為了交給它啟動的官方 Claude Code,從不顯示。
- 回報漏洞:[SECURITY.md](SECURITY.md)。

## 交給你的 AI App 安裝

AI App 幫你安裝 brunna 的過程中，有些事會直接問你，不經過對話：螢幕上跳出的視窗問你「是/否」(安裝軟體、改你
使用者層級的設定、允許 agent 接上哪個資料夾),以及瀏覽器裡開一個頁面讓你輸入金鑰。請在那裡回答，不要貼進對話。

給 agent 的步驟在英文版 README 的「Set up through your agent」;貼上的那段話會叫 agent 去照著做。

## 文件(英文)

| 內容 | 文件 |
|---|---|
| 安裝、需求、會睡眠的電腦、檔案放在哪裡、移除 | [docs/install.md](docs/install.md) |
| brunna 做的每件事 | [docs/features.md](docs/features.md) |
| 大家怎麼用:各 agent、有沒有助理 | [docs/user-stories.md](docs/user-stories.md) |
| 搭配 herdr 使用,以及給其他工具用的狀態摘要(`brunna glance`) | [docs/herdr.md](docs/herdr.md)(英文) |
| 日常指令 | [docs/commands.md](docs/commands.md) |
| 更新與退回 | [docs/upgrading.md](docs/upgrading.md) |
| 給任何 agent 用的信箱(MCP) | [docs/mcp.md](docs/mcp.md) |
| 無人看管的工作要用的登入(`brunna credentials`) | [docs/credentials.md](docs/credentials.md) |
| 每個部分的設計 | [docs/design/](docs/design) |
| 跟其他系統共用的介面 | [contracts/](contracts) |
| 參與開發(人和 agent) | [CONTRIBUTING.md](CONTRIBUTING.md)、[AGENTS.md](AGENTS.md) |
| 每個版本改了什麼 | [CHANGELOG.md](CHANGELOG.md) |
| 目前踩過的坑 | [known-issues.md](known-issues.md) |

## 授權

[Apache-2.0](LICENSE),包括 `contracts/` 裡的介面，以及被複製到其他 repo 的那些副本。
