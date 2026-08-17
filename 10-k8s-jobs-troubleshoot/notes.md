# Kubernetes — Job, CronJob, HPA & Troubleshooting

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành + giải thích đầy đủ ở [k8s-jobs-troubleshoot.md](k8s-jobs-troubleshoot.md).

## Job

<details>
<summary>1. Job khác Deployment ở mục đích? restartPolicy được dùng gì?</summary>

Deployment giữ Pod **luôn sống** (service); Job chạy task rồi **kết thúc** (batch/backup/migration). Pod Job
`Completed` **nằm lại không tự xoá** (để đọc log), chỉ mất khi `delete job`. `restartPolicy` chỉ được `Never`
hoặc `OnFailure` — KHÔNG `Always` (Always = ý "chạy mãi", vô nghĩa với Job).
</details>

<details>
<summary>2. <code>completions:4, parallelism:2</code> nghĩa là gì? (thực chạy)</summary>

Cần **4 Pod succeed** để Job Done, nhưng tối đa **2 Pod chạy song song** → 2 đợt. Lab thật: `COMPLETIONS` nhảy
`0/4→2/4→4/4`; cột `AGE` của Pod chia 2 nhóm (2 Pod AGE 24s + 2 Pod AGE 16s) = bằng chứng 2 đợt. `backoffLimit:3`
= fail quá 3 lần retry → Job Failed. `activeDeadlineSeconds` = trần thời gian toàn Job.
</details>

## CronJob

<details>
<summary>3. CronJob là gì? Cron 5 trường đọc thế nào? <code>0 3 * * 1-5</code>?</summary>

CronJob = Job + `schedule` (cron 5 trường) → tự sinh Job theo lịch. 5 trường: **phút giờ ngày-tháng tháng thứ**.
`0 3 * * 1-5` = 3:00 sáng thứ Hai→Sáu. `*/1 * * * *` = mỗi phút; `0 22 * * 1` = 22:00 thứ Hai.
</details>

<details>
<summary>4. concurrencyPolicy 3 giá trị? Vì sao Job cũ tự biến mất? (thực chạy)</summary>

`Allow` (mặc định, chạy chồng), `Forbid` (bỏ lịch mới nếu Job cũ chưa xong), `Replace` (kill Job cũ, chạy mới).
**CronJob tự GC Job cũ**: mặc định giữ `successfulJobsHistoryLimit: 3` (+ `failedJobsHistoryLimit: 1`) → lab
thật `get jobs` chỉ còn 3 Job mới nhất, Job cũ bị xoá (nên `logs job/<tên cũ>` báo NotFound). Tên Job =
`<cronjob>-<unix-minute>` (số phút Unix liên tiếp).
</details>

## HPA

<details>
<summary>5. HPA cần component gì bắt buộc? Vì sao Deployment phải khai resource request?</summary>

Bắt buộc **Metrics Server** — không có thì `kubectl top` lỗi, HPA hiện `<unknown>`, không scale. Deployment phải
khai `resources.requests.cpu` vì HPA tính **% dựa trên request** (vd request 100m, target 50% = 50m). Không có
request → HPA không tính được %. OrbStack cài Metrics Server cần `--kubelet-insecure-tls` (kubelet self-signed cert).
</details>

<details>
<summary>6. Công thức HPA? Vì sao lab dừng ở 2 Pod dù còn tải? (thực chạy)</summary>

`desiredReplicas = ceil(currentReplicas × currentCPU / targetCPU)`. Lab: 1 Pod CPU 68% → `ceil(1×68/50)=2` →
lên 2. Sau đó tải chia đôi → mỗi Pod 35% → `ceil(2×35/50)=2` → **giữ 2** (35%<50%). Không kẹt — đã **đạt cân
bằng** (đúng số Pod cần, CPU quanh ngưỡng). Muốn lên nữa phải tăng tải. Scale out nhanh (~30s), scale in chậm
(cooldown ~5 phút) tránh flapping. Trần cứng `maxReplicas`.
</details>

## Monitoring

<details>
<summary>7. 3 tầng monitoring khác nhau thế nào?</summary>

**Metrics Server**: CPU/mem real-time từ kubelet → `kubectl top` + HPA (ngắn hạn). **Prometheus**: scrape +
lưu **time-series** dài hạn + alerting. **Grafana**: dashboard trực quan query từ Prometheus. `kubectl top` cho
usage **thật** của node/Pod (khác `describe node` chỉ cho requests/limits).
</details>

## Troubleshooting

<details>
<summary>8. Luồng chẩn đoán vàng 5 bước theo thứ tự?</summary>

`get pods` (STATUS) → `describe pod` (Events — lý do rõ nhất) → `logs` (app log hiện tại) → `logs --previous`
(log lần crash trước) → `get events --sort-by=.lastTimestamp` (toàn cluster). Dừng ở bước nào ra manh mối thì
xử ngay. `exec -it -- sh` = vào shell test DNS/curl khi Pod Running mà app lỗi logic.
</details>

<details>
<summary>9. ErrImagePull vs CrashLoopBackOff — lệnh tiếp theo là gì? (thực chạy)</summary>

**ErrImagePull/ImagePullBackOff** (image tag sai/registry lỗi/không quyền pull) → `describe pod` → **Events tự
khai**: `Failed to pull image ... not found`. **CrashLoopBackOff** (app tự crash, restart mãi) → `describe` →
`Last State: Terminated, Exit Code: N`. ErrImagePull = vừa fail lần đầu; ImagePullBackOff = đã fail nhiều lần
đang đợi retry (cùng nguyên nhân).
</details>

<details>
<summary>10. <code>logs --previous</code> khác <code>logs</code>? Vì sao đôi khi KHÔNG lấy được? (thực chạy)</summary>

`logs` = container **đang chạy**; `--previous` = **lần chạy trước** (đã chết) — cần khi Pod vừa crash+restart.
Nhưng với CrashLoop restart nhanh, `--previous` thường báo `unable to retrieve container logs` vì container cũ
đã bị **containerd GC** (mỗi restart = container ID mới, log cũ bị dọn). → **`describe` là dự phòng chắc chắn**
(`Last State` + Exit Code luôn có). Đó là lý do luồng vàng đặt describe TRƯỚC logs.
</details>

## Bắc cầu sang production

<details>
<summary>11. Job/CronJob/HPA/troubleshoot dùng ở cụm thật thế nào?</summary>

- **Job**: DB migration (chạy 1 lần trước deploy), batch xử lý, backup snapshot.
- **CronJob**: backup DB đêm, dọn log cũ, gửi report định kỳ, sync dữ liệu (interval ≥1 phút; sub-minute →
 worker Deployment sleep-loop).
- **HPA**: API/service traffic biến động; cần Metrics Server. Kiểm soát nâng cao (custom metrics, scale theo
 queue length) → KEDA / Prometheus Adapter.
- **Troubleshoot**: luồng vàng dùng mỗi ngày. Production thêm Prometheus alert (OOM trend, restart rate) để bắt
 lỗi *trước* khi Pod chết.

| Module này | Kubernetes production |
|---|---|
| Job chạy-tới-xong | migration, batch, backup |
| CronJob + auto-GC | tác vụ định kỳ, tự dọn lịch sử |
| HPA + Metrics Server | tự scale theo tải, tiết kiệm tài nguyên |
| describe → logs --previous | quy trình debug chuẩn mỗi ngày |
</details>
