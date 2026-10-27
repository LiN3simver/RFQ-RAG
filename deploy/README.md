# 一鍵部署

傳輸方式是 `git push` 到伺服器上的 bare repo，`post-receive` hook 呼叫 `deploy/apply.sh`。

```bash
bash deploy/deploy.sh bootstrap   # 第一次用時跑一次（可重複執行）
bash deploy/deploy.sh plan        # 這次會傳什麼、伺服器會做什麼（唯讀）
bash deploy/deploy.sh deploy      # 真的部署（先印 plan 讓你確認）
bash deploy/deploy.sh status      # 伺服器現在跑哪一版、健不健康
bash deploy/deploy.sh rollback    # 回到上一個健康的版本
```

- **只傳版控裡的東西**：`.env`、`data/`、資料庫都不在版控裡，部署再多次都不會被動到。
- **改了什麼 → 要做什麼是算出來的**：比對新舊 commit 的檔案清單，決定要重建 image、
  只重啟，還是只提醒（資料庫結構變更只提醒，不自動執行）。規則在
  [target.conf](target.conf)，伺服器位址也在那裡（預設是佔位值，請改成自己的）。
- **秒級回滾、不需要網路**：每次 build 前把正在跑的 image 貼上 commit sha 標籤，
  回滾是換標籤，不是重新 build。
- **部署門檻**：[smoke.sh](smoke.sh) 在 api 容器裡檢查 `/readyz` 要 200、
  真的查一筆精確與一筆語意要有結果、image 裡的 agentkit 要跟版本紀錄一致；
  沒過就自動回滾。
