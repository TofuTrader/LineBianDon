# 便當訂購 Line Bot — 部署說明

## 系統功能

- 任何人發菜單 → 機器人廣播給所有成員
- 成員私訊機器人回覆訂購，自動記錄到 Google Sheet
- 同一人當天重複訂購，前筆自動標為「銷單」
- 每天 10 點自動結單：統計便當數量、扣除儲值、發個人通知

---

## 部署步驟

### 第一步：建立 Line Bot

1. 前往 [LINE Developers Console](https://developers.line.biz/console/)
2. 建立 Provider → 建立 Messaging API Channel
3. 在 Channel 設定頁：
   - 複製 **Channel Secret**
   - 在「Messaging API」分頁，Issue **Channel Access Token（長期）**
4. 關閉 **Auto-reply messages**（設為 Disabled）
5. 關閉 **Greeting messages**（設為 Disabled）

---

### 第二步：建立 Google Sheets

1. 到 [Google Sheets](https://sheets.google.com) 建立新試算表
2. 建立以下工作表頁籤（名稱要完全相同）：

   **儲值總表**（手動輸入）
   | 姓名 | 餘額 |
   |------|------|
   | 小明 | 500  |
   | 小華 | 300  |

   **訂單紀錄**（機器人自動寫入，建立空白頁籤即可）

   **使用者清單**（機器人自動建立，不需手動建立）

3. 複製試算表 URL 中的 **Spreadsheet ID**（網址中 `/d/` 和 `/edit` 之間那段）

---

### 第三步：建立 Google Service Account

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立新專案（或使用現有專案）
3. 啟用 **Google Sheets API** 和 **Google Drive API**
4. 前往「IAM & Admin」→「Service Accounts」→ 建立 Service Account
5. 建立後，在「Keys」分頁 → Add Key → JSON → 下載 JSON 金鑰檔
6. **將整個 JSON 檔案內容**備用（等等貼到 Render 環境變數）
7. 在 Google Sheets 試算表，點右上角「Share」→ 把 Service Account 的 email（格式：`xxx@xxx.iam.gserviceaccount.com`）加入，給予「Editor」權限

---

### 第四步：部署到 Render

1. 將本專案上傳到 [GitHub](https://github.com)（建立新 repo，上傳所有檔案）
2. 前往 [Render](https://render.com) → New Web Service → 連結 GitHub repo
3. 設定如下：
   - **Runtime**：Python
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`gunicorn app:app --bind 0.0.0.0:$PORT`
4. 在「Environment Variables」加入以下環境變數：

   | 變數名稱 | 內容 |
   |---------|------|
   | `LINE_CHANNEL_SECRET` | Line Channel Secret |
   | `LINE_CHANNEL_ACCESS_TOKEN` | Line Channel Access Token |
   | `GOOGLE_CREDENTIALS_JSON` | Google Service Account JSON 金鑰**全文**（貼上整個 JSON 內容） |
   | `SPREADSHEET_ID` | Google Sheet 的 ID |
   | `CLOSE_HOUR` | 結單時間（預設 10，代表早上 10 點） |
   | `TIMEZONE` | `Asia/Taipei` |

5. 部署完成後，複製 Render 給你的網址（例如：`https://linebot-bento.onrender.com`）

---

### 第五步：設定 Line Webhook

1. 回到 LINE Developers Console → Messaging API 分頁
2. **Webhook URL** 填入：`https://你的render網址/callback`
3. 點「Verify」確認成功
4. 開啟 **Use webhook**

---

## 使用方式

### 成員加入
- 掃描 QR Code 或搜尋 Bot ID 加好友
- 機器人會要求輸入姓名，輸入後即完成登記

### 發布菜單（任何人皆可）
```
【菜單】雞腿飯 80 / 排骨飯 75 / 素食便當 70
```
機器人會自動廣播給所有已登記的人。

### 訂購便當
收到廣播後，私訊機器人：
```
雞腿飯
```
或輸入編號：
```
1
```
若要取消：
```
不訂
```
若同一天重複訂購，前一筆自動銷單，以最後一筆為準。

### 儲值總表維護
直接在 Google Sheet「儲值總表」手動修改金額即可。

---

## 結單流程（每天 10 點自動執行）

1. 統計各便當種類數量與總金額
2. 對每位有訂便當的人發送個人通知（訂了什麼、扣多少、剩多少）
3. 廣播整體統計給所有人

---

## 注意事項

- Render 免費方案閒置一段時間後會進入休眠，第一次收到訊息可能有 30-60 秒延遲。若需要穩定服務，建議升級付費方案或使用 Uptime Robot 定期 ping。
- `GOOGLE_CREDENTIALS_JSON` 整個 JSON 要貼在一行，不能有換行。
