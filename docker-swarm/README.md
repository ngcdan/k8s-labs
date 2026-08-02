# Docker Swarm lab — multi-node trên Multipass

Bài lab dựng một cụm **Docker Swarm** nhiều node bằng Multipass, deploy cùng một
microservices app theo cả hai mô hình (imperative `service` và declarative `stack`),
và quan sát self-healing ở cả cấp container lẫn cấp node. Đây là bước dẫn nhập cho tư duy
declarative + desired-state reconciliation trước khi sang Kubernetes.

## Mục tiêu

**Tổng:** tự tay dựng cụm swarm nhiều node, deploy app theo cả imperative lẫn declarative,
thấy tận mắt self-healing; kết thúc phải trả lời được vì sao declarative thắng imperative
và vì sao đây là gốc của Kubernetes.

Mục tiêu học cụ thể:

1. **Ảo hóa multi-node** — dựng các VM bằng Multipass + cloud-init (không dùng blueprint
   `docker` đã deprecated và kẹt sàn RAM 4 GB); hiểu vì sao mỗi VM có nhiều IPv4 và vì sao
   `--advertise-addr` là bắt buộc khi init swarm.
2. **Kiến trúc cụm** — phân biệt manager vs worker, vai trò raft/quorum, và `drain` manager
   để giữ sạch control plane.
3. **Imperative — `docker service`** — create/scale; chứng minh ingress routing mesh
   (gọi vào manager đã drain vẫn tới app) và self-healing cấp container (giết container →
   reconciliation loop dựng bù).
4. **Declarative — `docker stack`** — deploy stack 2 service (`web-fe` + `redis`) từ một file
   YAML với image có sẵn trên registry; hiểu vì sao worker phải pull được từ registry; sửa
   `replicas` trong YAML rồi re-deploy thay cho `docker service scale` (tư duy GitOps).
5. **Self-healing cấp node** — xoá một node → replica tự dồn sang node còn sống.
6. **Rút ra nguyên lý** — declarative + desired-state reconciliation là gốc của Kubernetes.

## Kiến trúc cụm

5 VM Ubuntu 24.04, mỗi VM `--cpus 2 --memory 2G --disk 12G`, Docker cài qua cloud-init.

| Node | Vai trò | Ghi chú |
|---|---|---|
| node1 | manager (Leader) | nơi build/push image, init swarm, deploy stack |
| node2, node3 | manager (Reachable) | đủ 3 manager cho quorum raft |
| node4, node5 | worker | nơi app thật sự chạy sau khi drain manager |

Ngân sách RAM: OS ~6 GB + 5×2 GB = 16 GB, chạy tốt trên máy lab macOS Apple Silicon ≥ 24 GB.
Máy ít RAM hơn thì giảm còn 3 node (1 manager + 2 worker).

## Luồng bài

1. Cài Multipass trên máy lab.
2. Tạo 5 VM bằng cloud-init.
3. Init swarm trên node1 (`--advertise-addr` IP mạng Multipass), join manager + worker,
   drain 3 manager.
4. Imperative: `docker service create` + scale + thí nghiệm giết container.
5. Declarative: `docker stack deploy` từ image có sẵn → sửa YAML re-deploy → mô phỏng
   chết node.
6. Dọn: `docker stack rm` + `multipass delete` toàn bộ VM.

## Ngoài phạm vi

Overlay network nâng cao, swarm secrets/configs, TLS rotation, CI/CD thật — không đụng trong
bài này.

## Cấu trúc folder

| Đường dẫn | Là gì |
|---|---|
| [README.md](README.md) | Mục tiêu + tổng quan luồng |
| `runbook.md` | Các bước chi tiết, bổ sung dần khi thực hành |
| `notes.md` | Câu hỏi ôn tập recall (fold `<details>` + giải thích + hình) |
| `assets/` | Ảnh screenshot các bước thực hành |

## Câu hỏi kiểm tra

1. Vì sao `docker container ls` trên node1 không thấy app, mà `docker service ps web` thấy đủ?
2. Vì sao gọi vào IP của manager (đã drain, không chạy container nào) vẫn tới được app?
3. Nếu không push image lên registry mà chỉ build trên node1, `docker stack deploy` hỏng ở đâu?
4. Sửa `replicas` trong file rồi deploy lại — khác gì `docker service scale` về kết quả và
   về vận hành?
5. Xoá node5 xong, replica dồn hết sang node4. Nếu xoá luôn node4 thì sao?
