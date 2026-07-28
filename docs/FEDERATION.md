# Federation（M5）— 多節點共享 org 視圖

Bastet 的 federation 直接搭在 [Agent Memory OS](https://github.com/yamantaka520/Agent-Memory-OS)
的同步機制上：**AMOS 負責讓 teams / projects / members（以及記憶）跨節點收斂，
Bastet 呈現這份共享 org、並讓你把同步過來的專案綁定到本機 repo。**

## 什麼會同步、什麼不會

| 狀態 | 歸屬 | 跨節點 |
|---|---|---|
| Teams / Projects / Members | AMOS org（唯一事實來源，D3/D7） | ✅ AMOS federation 收斂（LWW + tombstones，含撤銷傳播） |
| 記憶（team/project scope） | AMOS | ✅ 同上，ACL 硬性把關 |
| Bastet 專案綁定（repo 路徑）、resources、grants、jobs、runs、ledger | Bastet（每節點的 `bastet.db`） | ❌ 刻意 per-node —— 資源與預算是本機治理的（SPEC §8 列有跨節點同步的未來待議） |

## 設定步驟

1. **兩台機器都安裝並設定 AMOS federation**（peer sync 或 bundle 交換），
   讓 org 結構收斂 —— 作法見 AMOS 的
   [Federation 文件](https://github.com/yamantaka520/Agent-Memory-OS#federation-multi-host-sync)。
   Bastet 對同步機制零介入：只要兩邊的 `agent-memory` 看得到同一份 org 即可。
2. 兩台機器各自 `bastet init && bastet serve`。
3. 在 Web UI「組織」頁的 **Federation** 區塊（或 `GET /api/org`）可以看到
   完整的 AMOS org 樹：本機已綁定的專案顯示 🔗，
   從其他節點同步過來、尚未在本機設定的專案顯示 ◌。
4. 對 ◌ 專案按 **bind…** 填入本機 repo 路徑（或
   `POST /api/org/bind {"project_id": ..., "repo_path": ...}`）——
   綁定後即可在本機對它派工；成員與記憶 ACL 自動沿用 AMOS 的那份。

## 語意注意

- 綁定是**本機**動作：兩台機器可以把同一個 AMOS 專案綁到各自的 repo 副本，
  各自派工、各自計費（grants 不共享）。
- 刪除 AMOS 專案（任一節點）經 federation 傳播後，該專案在 org 視圖消失；
  本機的 Bastet 綁定與歷史（jobs/runs/ledger）保留，顯示於 local-only 清單。
- Bastet 自有狀態（資源視圖、用量彙總）的跨節點同步是未來工作
  （SPEC §8），屆時預計借 AMOS bundle 通道。
