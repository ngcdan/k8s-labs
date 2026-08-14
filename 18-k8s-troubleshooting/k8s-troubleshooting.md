# 18 · Troubleshooting cụm — chẩn lỗi theo tầng, phá rồi sửa

> **Chặng 7 · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: Cluster upgrade · kế tiếp: Ingress stack (platform) · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** thành thạo luồng chẩn đoán từ triệu chứng → tầng → lệnh cụ thể; tự tay phá 4 loại sự cố thật (app crash, node down, control-plane hỏng, network/DNS) rồi sửa lại dưới đồng hồ; hiểu vì sao troubleshooting K8s là kỹ năng luyện bằng reps.
**Nền:** đã qua Pod, Deployment, Service, networking cơ bản, storage — lab này tổng hợp mọi tầng, không giới thiệu thêm khái niệm mới mà đào sâu vào vận hành thực.

> ⚠ **Lưu ý:** phần phá node/control-plane cần cụm multipass ở **lab 15** (cấu hình cho Mac Mini M4 24 GB: xem "Lưu ý phần cứng" đầu lab 15); mục app-level chạy được trên kind/OrbStack đang bật. **Output là MẪU chuẩn theo hành vi thật — CHƯA chạy trên máy bạn; verify khi phá & sửa thật.**

## Tiền đề
Cụm kubeadm đang chạy với ít nhất 1 control-plane và 1 worker. Kiểm tra điểm xuất phát sạch:

```bash
kubectl get nodes                         # tất cả Ready
kubectl get pods -A                       # không pod Pending/Error kỳ lạ
kubectl get pods -n kube-system           # coredns, kube-proxy, etcd, apiserver Running
```

```text
$ kubectl get nodes
NAME        STATUS   ROLES           AGE   VERSION
cp-node     Ready    control-plane   3d    v1.29.3
worker-1    Ready    <none>          3d    v1.29.3

$ kubectl get pods -n kube-system
NAME                               READY   STATUS    RESTARTS   AGE
coredns-5dd5756b68-4wtvl           1/1     Running   0          3d
coredns-5dd5756b68-s9gpq           1/1     Running   0          3d
etcd-cp-node                       1/1     Running   0          3d
kube-apiserver-cp-node             1/1     Running   0          3d
kube-controller-manager-cp-node   1/1     Running   0          3d
kube-proxy-7cxzp                   1/1     Running   0          3d
kube-proxy-nhmrd                   1/1     Running   0          3d
kube-scheduler-cp-node             1/1     Running   0          3d
```

Tinh thần lab: sau mỗi mục "Làm" sẽ có bước **phá** (gây sự cố) → **chẩn** (tìm nguyên nhân) → **sửa** (khôi phục). Đặt đồng hồ đếm ngược 15 phút mỗi mục để tập phản xạ.

---

## 1. Luồng chẩn đoán vàng

**Chốt:** Mọi sự cố K8s đều chẩn theo cùng một luồng: `kubectl describe` (Events) → `logs [--previous]` → `get events --sort-by=.lastTimestamp` → xuống node nếu cần. Đọc từ **triệu chứng → tầng nghi ngờ**, không đoán mò.

- `kubectl describe <resource>` — Events cuối mô tả hành động gần nhất; thường chứa thông báo lỗi trực tiếp từ scheduler, kubelet, container runtime.
- `kubectl logs <pod> [--previous]` — stdout/stderr app. `--previous` = lần chạy trước khi crash, không có flag này sẽ thấy container mới (đã restart) không có gì.
- `kubectl get events -n <ns> --sort-by=.lastTimestamp` — bức tranh toàn cảnh namespace, hữu ích khi không biết pod nào đang lỗi.
- `kubectl get pods -A --field-selector=status.phase!=Running` — quét nhanh toàn cụm, lọc pod không ở trạng thái Running.

**Vì sao:** K8s có nhiều tầng (app → pod → node → network → control-plane). Không có luồng cố định sẽ nhảy cóc, bỏ qua manh mối, tốn thời gian. Luồng vàng này dựa trên mức độ phổ biến của nguyên nhân: phần lớn lỗi là app config sai (tầng app) → ít hơn là node hỏng → hiếm là control-plane crash.

**Cơ chế:** Events được kubelet, scheduler, controller-manager ghi vào etcd qua API server. Chúng có TTL ~1 giờ (mặc định `--event-ttl=1h` trên apiserver), nên sự cố cũ sẽ không còn event — lúc đó chuyển sang `journalctl` trên node. `logs --previous` đọc log từ file trên node mà kubelet lưu lại cho container đã kết thúc; nếu node đã restart hoặc log bị rotate thì mất.

> 💡 **Ẩn dụ:** Chẩn đoán K8s như bác sĩ cấp cứu — không khám đầu đến chân ngẫu nhiên, mà đọc dấu hiệu sinh tồn (Events, Status) → nghi tầng → xét nghiệm chính xác (`logs`, `exec`). Nhảy thẳng vào "chắc là network" mà bỏ qua Events = khám chân khi bệnh nhân kêu đau đầu.

**Bảng triệu chứng → tầng nghi ngờ:**

| Triệu chứng quan sát được | Tầng nghi ngờ đầu tiên | Lệnh tiếp theo |
|---|---|---|
| Pod `CrashLoopBackOff` | App crash / config sai | `logs --previous`, `describe` Events |
| Pod `ImagePullBackOff` / `ErrImagePull` | Registry / image name sai | `describe` → Events → kiểm tra image name/tag/secret |
| Pod `Pending` mãi không schedule | Scheduler / node resource | `describe pod` Events (`FailedScheduling`) |
| Node `NotReady` | kubelet / network plugin | `systemctl status kubelet`, `journalctl -u kubelet` trên node đó |
| `kubectl` treo / trả `connection refused` | API server / control-plane | `crictl ps` trên master, log trong `/var/log/pods` |
| Service không route đến Pod | Selector sai / endpoints rỗng / NetworkPolicy | `get endpoints <svc>`, `describe networkpolicy` |
| Pod resolve tên không được | CoreDNS | `kubectl exec` pod tạm → `nslookup`, `kubectl -n kube-system get pods` |

**Dùng / không dùng:**
- Luôn bắt đầu bằng `describe` + `logs` trước khi xuống tầng node — 80% lỗi nằm ở tầng app/config, không cần SSH vào node.
- **Phản đề:** đừng dừng ở `get pods` thấy `Running` rồi kết luận "ổn" — Pod Running không có nghĩa app đang hoạt động đúng (readiness probe có thể không chính xác, hay app đang loop lỗi mà không crash process).

**Làm:**

```bash
# quét nhanh toàn cụm — pod nào không Running?
kubectl get pods -A --field-selector=status.phase!=Running

# xem events toàn namespace default, mới nhất lên trên
kubectl get events -n default --sort-by=.lastTimestamp | tail -20

# tạo pod lỗi để thực hành luồng
kubectl run crash-test --image=nginx:alpine \
  --command -- /bin/sh -c 'echo "starting"; sleep 5; exit 1'
sleep 10
kubectl get pod crash-test
kubectl describe pod crash-test | grep -A 20 Events
kubectl logs crash-test --previous
```

**Kết quả:**

```text
$ kubectl get pod crash-test
NAME         READY   STATUS             RESTARTS   AGE
crash-test   0/1     CrashLoopBackOff   2          35s

$ kubectl describe pod crash-test | grep -A 20 Events
Events:
  Type     Reason     Age               From               Message
  ----     ------     ----              ----               -------
  Normal   Scheduled  38s               default-scheduler  Successfully assigned default/crash-test to worker-1
  Normal   Pulled     37s               kubelet            Successfully pulled image "nginx:alpine" in 1.4s
  Normal   Created    37s               kubelet            Created container crash-test
  Normal   Started    37s               kubelet            Started container crash-test
  Warning  BackOff    8s (x3 over 28s)  kubelet            Back-off restarting failed container crash-test

$ kubectl logs crash-test --previous
starting
```

→ **Verify:** `CrashLoopBackOff` + `BackOff` event + `--previous` thấy "starting" (log lần crash trước). Container chết ngay sau 5 giây do `exit 1`.

![[troubleshoot-tree.excalidraw]]

---

## 2. Application failure

**Chốt:** Lỗi tầng app là nguyên nhân phổ biến nhất. Bốn dạng: **CrashLoopBackOff** (app crash), **ImagePullBackOff** (image sai), **config không tìm thấy** (env/secret/configmap missing), **readiness fail** → Service không route.

- `CrashLoopBackOff`: container chạy → crash → restart → crash → K8s tăng delay back-off (10s → 20s → 40s → …) trước mỗi lần retry. `logs --previous` cho stack trace lần crash trước.
- `ImagePullBackOff` / `ErrImagePull`: image name sai, tag không tồn tại, hoặc private registry thiếu `imagePullSecrets`. Events sẽ hiện `Failed to pull image`.
- Config missing: nếu container tham chiếu Secret/ConfigMap không tồn tại → Pod ở `Pending` (nếu `envFrom`) hoặc start xong rồi crash vì biến môi trường rỗng.
- Readiness fail → K8s gỡ Pod khỏi `Endpoints` → Service không route → client nhận lỗi kết nối dù Pod đang `Running`.

**Vì sao:** K8s không đọc nội dung image hay config trước khi schedule — chỉ biết lúc container thật sự chạy. Hiểu luồng retry (back-off) giúp phân biệt "đang thử lần đầu" vs "đã crash nhiều lần, sắp chờ lâu hơn".

**Cơ chế:** kubelet gọi container runtime (containerd) `pull` image → nếu pull lỗi ghi Event `Failed to pull image` và set status `ImagePullBackOff`. Với CrashLoopBackOff: kubelet restart container sau delay tăng dần (max 5 phút); `RESTARTS` tăng trong `kubectl get pods`. Config missing (`envFrom secretRef`): API server kiểm tra secret tồn tại khi mount — Pod không được tạo (stuck `ContainerCreating` hoặc `Pending`) nếu secret thiếu.

> 💡 **Ẩn dụ:** CrashLoopBackOff = nhân viên vào ca → bất tỉnh → hồi sức → vào ca → bất tỉnh — lặp với khoảng nghỉ ngày càng dài. `logs --previous` = đọc nhật ký ca làm việc cuối trước khi bất tỉnh.

**Dùng / không dùng:**
- `logs --previous` — **luôn dùng trước** khi làm gì khác với CrashLoopBackOff.
- Với ImagePullBackOff: kiểm tra chính tả image + tag; thử `docker pull <image>` trực tiếp trên node để loại trừ network.
- **Phản đề:** không nên tăng `restartPolicy: Never` để chặn restart nhằm "đọc log" — log vẫn đọc được bằng `--previous`. Chặn restart làm mất khả năng tự hồi của app sau fix.

**Làm:**

```bash
# --- Kịch bản A: CrashLoopBackOff ---
kubectl run bad-exit --image=busybox -- /bin/sh -c 'echo "FATAL: db connection refused"; exit 1'
sleep 15
kubectl get pod bad-exit
kubectl logs bad-exit --previous

# --- Kịch bản B: ImagePullBackOff ---
kubectl run bad-image --image=nginx:nonexistent-tag-9999
sleep 10
kubectl describe pod bad-image | grep -A 10 Events

# --- Kịch bản C: Secret missing ---
cat > /tmp/secret-missing.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: secret-missing
spec:
  containers:
  - name: app
    image: busybox
    command: ["sleep", "3600"]
    envFrom:
    - secretRef:
        name: db-credentials   # secret này không tồn tại
EOF
kubectl apply -f /tmp/secret-missing.yml
sleep 5
kubectl get pod secret-missing
kubectl describe pod secret-missing | grep -A 10 Events

# --- Kịch bản D: Readiness fail → Service không route ---
cat > /tmp/readiness-fail.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: readiness-fail
  labels:
    app: readiness-fail
spec:
  containers:
  - name: web
    image: nginx:alpine
    readinessProbe:
      httpGet:
        path: /nonexistent-path
        port: 80
      initialDelaySeconds: 2
      periodSeconds: 3
---
apiVersion: v1
kind: Service
metadata:
  name: readiness-svc
spec:
  selector:
    app: readiness-fail
  ports:
  - port: 80
    targetPort: 80
EOF
kubectl apply -f /tmp/readiness-fail.yml
sleep 10
kubectl get pod readiness-fail                  # READY 0/1
kubectl get endpoints readiness-svc             # <none>
kubectl describe pod readiness-fail | grep -A5 Readiness
```

**Kết quả:**

```text
# Kịch bản A
$ kubectl get pod bad-exit
NAME       READY   STATUS             RESTARTS   AGE
bad-exit   0/1     CrashLoopBackOff   3          45s

$ kubectl logs bad-exit --previous
FATAL: db connection refused

# Kịch bản B
$ kubectl describe pod bad-image | grep -A 10 Events
Events:
  Type     Reason          Age   From               Message
  ----     ------          ----  ----               -------
  Warning  Failed          12s   kubelet            Failed to pull image "nginx:nonexistent-tag-9999": rpc error: code = NotFound
  Warning  Failed          12s   kubelet            Error: ErrImagePull
  Normal   BackOff         8s    kubelet            Back-off pulling image "nginx:nonexistent-tag-9999"
  Warning  Failed          8s    kubelet            Error: ImagePullBackOff

# Kịch bản C
$ kubectl get pod secret-missing
NAME             READY   STATUS                       RESTARTS   AGE
secret-missing   0/1     CreateContainerConfigError   0          8s

$ kubectl describe pod secret-missing | grep -A 10 Events
Events:
  Warning  Failed  5s  kubelet  Error: secret "db-credentials" not found

# Kịch bản D
$ kubectl get pod readiness-fail
NAME             READY   STATUS    RESTARTS   AGE
readiness-fail   0/1     Running   0          12s

$ kubectl get endpoints readiness-svc
NAME            ENDPOINTS   AGE
readiness-svc   <none>      12s
```

→ **Verify:** Kịch bản A: `--previous` thấy log crash. B: Events rõ `ErrImagePull`. C: Pod stuck `CreateContainerConfigError`. D: Pod `Running` nhưng `READY 0/1`, endpoints `<none>` — Service không route dù pod tồn tại.

---

## 3. Node NotReady

**Chốt:** Node `NotReady` nghĩa là control-plane không nhận được heartbeat từ kubelet trên node đó. Nguyên nhân phổ biến: kubelet chết/crashed, network plugin lỗi, hoặc cert kubelet hết hạn. Chẩn bằng `systemctl status kubelet` + `journalctl -u kubelet` trực tiếp trên node đó.

- Node `NotReady` → pod trên node đó dần chuyển `Unknown` → K8s evict pod (mặc định sau 5 phút `--pod-eviction-timeout`).
- `kubectl describe node <name>` → mục `Conditions` và `Events` trên node; `kubectl get events` lọc theo node.
- Trên node bị lỗi: `systemctl status kubelet` (process còn sống không?), `journalctl -u kubelet -n 50 --no-pager` (50 dòng log gần nhất).
- Cert hết hạn: `kubeadm certs check-expiration` (chạy trên control-plane node).
- `/etc/kubernetes/kubelet.conf` hỏng hoặc trỏ sai cluster → kubelet không connect được API server.

**Vì sao:** kubelet là "đại lý" của K8s trên mỗi node — không có kubelet thì node đó là hộp đen với control-plane. Pod không được schedule mới lên node NotReady; pod đang chạy dần bị đánh dấu Unknown và evict. Đây là tình huống ảnh hưởng workload nghiêm trọng nhất sau control-plane failure.

**Cơ chế:** node-controller trong controller-manager giám sát heartbeat của từng node (kubelet gửi NodeStatus mỗi `--node-status-update-frequency=10s`). Nếu không nhận heartbeat trong `--node-monitor-grace-period=40s` → set condition `Ready=Unknown`. Sau `--pod-eviction-timeout=5m0s` → evict pod. Kubelet chết mà không restart tự động = node sẽ dần mất sạch workload.

> 💡 **Ẩn dụ:** Node = cửa hàng nhượng quyền, kubelet = điện thoại báo cáo doanh thu mỗi 10 giây. Tổng hành dinh (control-plane) không nhận báo cáo 40 giây → đánh dấu "cửa hàng mất liên lạc" (NotReady). Sau 5 phút không liên lạc được → rút hàng (evict pod) sang cửa hàng khác.

**Dùng / không dùng:**
- SSH vào node NotReady ngay — không thể chẩn từ xa qua `kubectl` vì kubelet không nhận lệnh.
- `journalctl -u kubelet -f` để theo dõi log real-time khi đang sửa.
- **Phản đề:** không restart kubelet blindly nếu chưa đọc log — nếu nguyên nhân là cert hết hạn thì restart cũng không giúp gì, phải renew cert trước.

**Làm (cần SSH vào worker node):**

```bash
# --- Trên control-plane: quan sát trạng thái hiện tại ---
kubectl get nodes
kubectl describe node worker-1 | grep -A 10 Conditions

# --- PHÁ: SSH vào worker-1, dừng kubelet ---
# (thay "worker-1" bằng tên node thật trong cụm bạn)
ssh worker-1
sudo systemctl stop kubelet
exit

# --- Trên control-plane: quan sát thay đổi (chờ ~45 giây) ---
kubectl get nodes -w
# Ctrl+C sau khi thấy NotReady

kubectl describe node worker-1 | grep -A 5 "Ready"
kubectl get events --field-selector=involvedObject.name=worker-1 \
  --sort-by=.lastTimestamp

# --- CHẨN: SSH lại worker-1 ---
ssh worker-1
sudo systemctl status kubelet
sudo journalctl -u kubelet -n 30 --no-pager
# Lưu ý: sau khi stop, log sẽ không có dòng mới — đây là manh mối "kubelet không chạy"
exit

# --- CHẨN cert (chạy trên control-plane) ---
sudo kubeadm certs check-expiration

# --- SỬA: SSH lại worker-1, khởi động kubelet ---
ssh worker-1
sudo systemctl start kubelet
sudo systemctl status kubelet    # Active: active (running)
exit

# --- Xác nhận trên control-plane (chờ ~15 giây) ---
kubectl get nodes
```

**Kết quả:**

```text
# Sau khi stop kubelet, chờ ~45 giây
$ kubectl get nodes -w
NAME        STATUS     ROLES           AGE   VERSION
cp-node     Ready      control-plane   3d    v1.29.3
worker-1    Ready      <none>          3d    v1.29.3
worker-1    NotReady   <none>          3d    v1.29.3   ← node mất heartbeat

$ kubectl describe node worker-1 | grep -A 5 "Ready"
  Ready            False   Mon, 13 Aug 2026 08:12:41   Mon, 13 Aug 2026 08:12:01   KubeletNotReady   container runtime network not ready: NetworkReady=false reason:NetworkPlugin
# (hoặc đơn giản hơn) Ready False ... kubelet stopped posting node status.

# SSH worker-1: systemctl status kubelet
● kubelet.service - kubelet: The Kubernetes Node Agent
     Loaded: loaded (/lib/systemd/system/kubelet.service; enabled)
     Active: inactive (dead) since Mon 2026-08-13 08:12:00 UTC; 1min 30s ago
   Main PID: 2341 (code=exited, status=0/SUCCESS)

# journalctl -u kubelet -n 30 (sau khi stop — log dừng tại thời điểm stop)
Aug 13 08:12:00 worker-1 kubelet[2341]: I0813 08:12:00 kubelet.go:2390] "SyncLoop PLEG" ...
Aug 13 08:12:00 worker-1 systemd[1]: Stopping kubelet: The Kubernetes Node Agent...
Aug 13 08:12:00 worker-1 systemd[1]: kubelet.service: Succeeded.

# Sau khi start lại, chờ ~15 giây
$ kubectl get nodes
NAME        STATUS   ROLES           AGE   VERSION
cp-node     Ready    control-plane   3d    v1.29.3
worker-1    Ready    <none>          3d    v1.29.3    ← Ready trở lại
```

→ **Verify:** node chuyển `NotReady` đúng ~40 giây sau khi stop kubelet; sau `systemctl start kubelet` node `Ready` trở lại trong vòng 15 giây. `kubeadm certs check-expiration` hiện thời gian còn lại của mọi cert.

---

## 4. Control-plane failure

**Chốt:** Khi API server chết, `kubectl` treo hoặc trả `connection refused` — không làm gì được qua `kubectl`. Chẩn bằng `crictl ps` (hoặc `docker ps`) trực tiếp trên master node; static pod manifest nằm trong `/etc/kubernetes/manifests/` — sửa file là kubelet tự dựng lại container.

- API server, etcd, controller-manager, scheduler đều là **static pod** — kubelet đọc manifest từ `/etc/kubernetes/manifests/*.yaml` và tự tạo/restart/xóa container mà không cần API server.
- Sửa manifest sai → kubelet dừng container cũ → tạo container mới từ manifest mới → nếu manifest hỏng thì container không start.
- Log static pod: `/var/log/pods/kube-system_kube-apiserver-<node>_*/kube-apiserver/*.log` (hoặc `crictl logs <container-id>`).
- `crictl ps -a` — liệt kê tất cả container (kể cả đã dừng), dùng khi không có `kubectl`.
- Khi `kubectl` trả `The connection to the server <IP>:6443 was refused` → API server chưa sẵn sàng.

**Vì sao:** static pod là cơ chế "tự khởi động" của control-plane — kubelet chạy trước cả API server, đọc manifest và dựng API server lên. Hiểu điều này tránh nhầm "phải có kubectl mới fix được". Trên môi trường thật, lỗi manifest (VD: update version apiserver image sai tag) là nguyên nhân phổ biến khiến apiserver không lên sau upgrade.

**Cơ chế:** kubelet có `--pod-manifest-path=/etc/kubernetes/manifests` (hoặc `staticPodPath` trong kubelet config). Kubelet watch thư mục này, nếu file thay đổi → kill container cũ → tạo container mới từ manifest mới. Không cần etcd, không cần API server — đây là vòng lặp hoàn toàn local. Nếu manifest YAML lỗi (syntax hoặc image không pull được) → container không start → apiserver không lên.

> 💡 **Ẩn dụ:** Static pod = máy phát điện dự phòng tự khởi (không cần điện lưới để chạy). Kubelet = bảo vệ tòa nhà đọc sơ đồ kỹ thuật (`/etc/kubernetes/manifests`) và tự vận hành generator. Nếu sơ đồ kỹ thuật sai → generator hỏng → cả tòa mất điện (không có API server → kubectl không vào được).

**Dùng / không dùng:**
- Chỉnh sửa manifest trong `/etc/kubernetes/manifests/` là cách duy nhất khi apiserver không lên — không thể dùng `kubectl apply`.
- `crictl` thay thế `docker` trên node dùng containerd.
- **Phản đề:** không nên xóa file manifest để "reset" — kubelet sẽ dừng static pod đó vĩnh viễn cho đến khi file được tạo lại. Nếu muốn tạm dừng apiserver để debug etcd, di chuyển file ra ngoài thư mục (`mv` đến `/tmp`) thay vì xóa.

**Làm (cần SSH vào control-plane node):**

```bash
# --- Ghi lại dòng image hiện tại để sửa lại sau ---
ssh cp-node
grep "image:" /etc/kubernetes/manifests/kube-apiserver.yaml | head -1
# ví dụ: image: registry.k8s.io/kube-apiserver:v1.29.3

# --- PHÁ: sửa image thành tag không tồn tại ---
sudo cp /etc/kubernetes/manifests/kube-apiserver.yaml \
        /tmp/kube-apiserver.yaml.bak          # backup trước khi phá

sudo sed -i 's|kube-apiserver:v1.29.3|kube-apiserver:v0.0.0-nonexistent|g' \
         /etc/kubernetes/manifests/kube-apiserver.yaml

# Rời khỏi SSH (lần tiếp theo kubectl sẽ treo)
exit

# --- Trên control-plane (hoặc máy có kubeconfig): quan sát kubectl treo ---
kubectl get nodes   # sẽ treo hoặc trả lỗi sau vài giây

# --- CHẨN: SSH lại cp-node ---
ssh cp-node

# Kiểm tra container apiserver có đang chạy không
sudo crictl ps | grep apiserver      # không thấy hoặc thấy Exited

# Xem container đã dừng (kể cả Exited)
sudo crictl ps -a | grep apiserver

# Lấy container ID và xem log
CONTAINER_ID=$(sudo crictl ps -a | grep apiserver | awk '{print $1}' | head -1)
sudo crictl logs $CONTAINER_ID 2>&1 | tail -20

# Hoặc xem log file trực tiếp
sudo ls /var/log/pods/ | grep apiserver
sudo tail -30 /var/log/pods/kube-system_kube-apiserver-cp-node_*/kube-apiserver/*.log

# --- SỬA: khôi phục manifest từ backup ---
sudo cp /tmp/kube-apiserver.yaml.bak \
        /etc/kubernetes/manifests/kube-apiserver.yaml

# Kubelet sẽ tự phát hiện thay đổi và dựng lại container (chờ ~30 giây)
sudo crictl ps | grep apiserver    # phải thấy Running
exit

# --- Xác nhận kubectl hoạt động lại ---
kubectl get nodes
```

**Kết quả:**

```text
# Sau khi sửa manifest sai, trên máy chạy kubectl
$ kubectl get nodes
The connection to the server <control-plane-IP>:6443 was refused - did you specify the right host or port?

# SSH cp-node: crictl ps sau khi manifest bị sửa
$ sudo crictl ps | grep apiserver
(không có dòng nào)

$ sudo crictl ps -a | grep apiserver
8f3a2b1c9d4e   registry.k8s.io/kube-apiserver:v0.0.0-nonexistent   Exited   2m   kube-apiserver

$ sudo crictl logs 8f3a2b1c9d4e 2>&1 | tail -5
Failed to pull image "registry.k8s.io/kube-apiserver:v0.0.0-nonexistent": rpc error: code = NotFound
Error: ErrImagePull

# Sau khi khôi phục manifest và chờ ~30 giây
$ sudo crictl ps | grep apiserver
a1b2c3d4e5f6   registry.k8s.io/kube-apiserver:v1.29.3   Running   20s   kube-apiserver

# kubectl hoạt động lại
$ kubectl get nodes
NAME        STATUS   ROLES           AGE   VERSION
cp-node     Ready    control-plane   3d    v1.29.3
worker-1    Ready    <none>          3d    v1.29.3
```

→ **Verify:** `kubectl` trả `connection refused` khi apiserver down; `crictl ps -a` thấy container `Exited` với lý do image không pull được; sau restore manifest kubelet tự dựng apiserver lại trong ~30 giây.

---

## 5. Network / DNS / kube-proxy

**Chốt:** Lỗi network K8s thường biểu hiện qua 3 dạng: **Pod không resolve tên service** (CoreDNS), **Service không route đến Pod** (selector sai → endpoints rỗng), **NetworkPolicy chặn nhầm**. Dùng pod tạm (`busybox`) để chẩn từ bên trong cluster network.

- **DNS không resolve:** `nslookup <svc-name>` trong pod tạm → nếu `NXDOMAIN` hoặc timeout thì CoreDNS lỗi hoặc cấu hình sai.
- **Endpoints rỗng:** `kubectl get endpoints <svc>` trả `<none>` → selector trong Service không khớp label của Pod nào.
- **NetworkPolicy chặn:** traffic thất bại nhưng pod và service đều ổn → kiểm tra `kubectl get networkpolicy -A` và `describe`.
- **kube-proxy:** nếu ClusterIP không route đến backend → `kubectl -n kube-system get pods | grep kube-proxy`, xem log kube-proxy DaemonSet.

**Vì sao:** network là tầng ẩn nhất — không có output trực tiếp như CrashLoopBackOff. Nhiều người mất giờ `ping` từ ngoài vào trong khi vấn đề chỉ là label selector sai một chữ. Dùng pod debug bên trong cluster (cùng network namespace với workload) cho kết quả chính xác hơn bất kỳ công cụ nào từ bên ngoài.

**Cơ chế:** CoreDNS chạy như Deployment trong `kube-system`, lắng nghe `53/UDP` trên ClusterIP `10.96.0.10` (mặc định). Mỗi Pod nhận `/etc/resolv.conf` trỏ đến CoreDNS ClusterIP. Service ClusterIP được implement bởi kube-proxy qua iptables/ipvs rules trên mỗi node — không phải container, không phải process nào nghe trên ClusterIP đó. Endpoints object là danh sách IP:port của Pod thực sự sẽ nhận traffic — rỗng thì ClusterIP không biết forward đi đâu.

> 💡 **Ẩn dụ:** ClusterIP = số điện thoại tổng đài (1800-xxx), Endpoints = danh sách máy lẻ nhân viên trực. Nếu danh sách rỗng (selector sai) thì dù gọi tổng đài đúng số, không ai bắt máy. CoreDNS = bộ danh bạ nội bộ — tra "ten-service" ra IP tổng đài. Nếu danh bạ hỏng → không tra được số.

**Dùng / không dùng:**
- Luôn test từ trong cluster (pod tạm `busybox`/`netshoot`) — test từ laptop ra ClusterIP sẽ không hoạt động vì ClusterIP chỉ routable trong cluster.
- `kubectl get endpoints <svc>` là bước đầu tiên khi Service không reach được Pod.
- **Phản đề:** NetworkPolicy dễ bị quên — nếu endpoints đúng mà vẫn không kết nối được, luôn kiểm tra NetworkPolicy trước khi đổ lỗi kube-proxy.

**Làm:**

```bash
# Tạo workload để thực hành
cat > /tmp/net-lab.yml <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend
spec:
  replicas: 2
  selector:
    matchLabels:
      app: backend
  template:
    metadata:
      labels:
        app: backend
    spec:
      containers:
      - name: web
        image: nginx:alpine
        ports:
        - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: backend-svc
spec:
  selector:
    app: backend-TYPO    # <-- selector sai cố ý: "backend-TYPO" != "backend"
  ports:
  - port: 80
    targetPort: 80
EOF
kubectl apply -f /tmp/net-lab.yml
sleep 10

# --- Kịch bản A: Selector sai → endpoints rỗng ---
kubectl get endpoints backend-svc            # phải thấy <none>
kubectl describe service backend-svc | grep Selector
kubectl get pods --show-labels | grep backend

# Sửa selector
kubectl patch service backend-svc --type='json' \
  -p='[{"op":"replace","path":"/spec/selector/app","value":"backend"}]'
kubectl get endpoints backend-svc            # phải thấy IP:80

# --- Kịch bản B: DNS không resolve ---
# Tạo pod tạm để test từ trong cluster
kubectl run dns-test --image=busybox:1.36 --restart=Never -it --rm \
  -- /bin/sh -c 'nslookup backend-svc; nslookup kubernetes'

# Nếu CoreDNS lỗi, kiểm tra
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=20

# --- Kịch bản C: NetworkPolicy block ---
cat > /tmp/deny-all.yml <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-ingress
  namespace: default
spec:
  podSelector:
    matchLabels:
      app: backend
  policyTypes:
  - Ingress
EOF
kubectl apply -f /tmp/deny-all.yml

# Test từ pod tạm — sẽ bị chặn (timeout)
kubectl run net-test --image=busybox:1.36 --restart=Never -it --rm \
  -- /bin/sh -c 'wget -qO- --timeout=3 http://backend-svc || echo "BLOCKED"'

# Chẩn NetworkPolicy
kubectl get networkpolicy -n default
kubectl describe networkpolicy deny-all-ingress

# Xóa policy để restore
kubectl delete networkpolicy deny-all-ingress
```

**Kết quả:**

```text
# Kịch bản A: endpoints rỗng
$ kubectl get endpoints backend-svc
NAME          ENDPOINTS   AGE
backend-svc   <none>      12s

$ kubectl describe service backend-svc | grep Selector
Selector:          app=backend-TYPO

$ kubectl get pods --show-labels | grep backend
backend-7d9f6c-xk2p4   1/1   Running   0   15s   app=backend,...
backend-7d9f6c-mn3r7   1/1   Running   0   15s   app=backend,...
# "backend" ≠ "backend-TYPO" → endpoints rỗng

# Sau khi patch selector:
$ kubectl get endpoints backend-svc
NAME          ENDPOINTS                         AGE
backend-svc   192.168.1.14:80,192.168.1.15:80   20s

# Kịch bản B: DNS từ trong cluster
$ kubectl run dns-test ... -- nslookup backend-svc
Server:    10.96.0.10
Address 1: 10.96.0.10 kube-dns.kube-system.svc.cluster.local

Name:      backend-svc
Address 1: 10.100.42.87 backend-svc.default.svc.cluster.local

# Kịch bản C: NetworkPolicy chặn
$ kubectl run net-test ... -- wget -qO- --timeout=3 http://backend-svc || echo "BLOCKED"
wget: download timed out
BLOCKED

$ kubectl get networkpolicy -n default
NAME               POD-SELECTOR   AGE
deny-all-ingress   app=backend    30s
```

→ **Verify:** A: endpoints `<none>` khi selector sai → `<IP>:80` sau khi sửa. B: `nslookup` trong pod tạm resolve được `backend-svc` thành ClusterIP. C: `wget` timeout khi NetworkPolicy chặn ingress.

---

## 🧹 Dọn dẹp

Khôi phục tất cả những gì đã phá trong lab:

```bash
# Xóa workload đã tạo trong lab
kubectl delete pod crash-test bad-exit bad-image secret-missing readiness-fail \
  --ignore-not-found

kubectl delete deployment backend --ignore-not-found
kubectl delete service backend-svc readiness-svc --ignore-not-found
kubectl delete networkpolicy deny-all-ingress --ignore-not-found

# Xóa file tạm
rm -f /tmp/secret-missing.yml /tmp/readiness-fail.yml \
      /tmp/net-lab.yml /tmp/deny-all.yml

# Xác nhận cụm sạch
kubectl get pods -A --field-selector=status.phase!=Running
kubectl get nodes   # tất cả Ready
```

---

## ✅ Đủ khi

① Nói được luồng chẩn đoán vàng (describe → logs → events) và chọn được tầng đúng từ triệu chứng — không mò mẫm.
② Với `CrashLoopBackOff` / `ImagePullBackOff` / secret missing — tìm ra nguyên nhân trong 2 phút chỉ bằng `describe` + `logs --previous`.
③ Phá kubelet trên worker node → xác nhận `NotReady` → SSH vào → `systemctl start kubelet` → node `Ready` trở lại.
④ Phá static pod apiserver (sửa manifest) → kubectl treo → SSH master → `crictl ps -a` thấy lý do → khôi phục manifest → apiserver sống lại.
⑤ Chẩn được endpoints rỗng (selector sai), DNS không resolve (CoreDNS), và NetworkPolicy chặn nhầm — từ trong cluster dùng pod tạm busybox.

---

## 🧠 Recall

1. Kể tên 3 lệnh trong "luồng chẩn đoán vàng" và thứ tự dùng chúng.
2. `CrashLoopBackOff` khác `Error` ở điểm nào? Tại sao cần flag `--previous` khi xem log?
3. Pod `Running` nhưng `READY 0/1` — nguyên nhân có thể là gì? Hậu quả với Service?
4. Node `NotReady` — 2 lệnh đầu tiên bạn chạy sau khi SSH vào node đó?
5. `kubeadm certs check-expiration` kiểm tra gì? Chạy trên node nào?
6. Static pod là gì? Kubelet đọc manifest từ đâu? Điều đó có nghĩa gì khi apiserver chết?
7. Khi `kubectl` trả `connection refused` — bước đầu tiên để chẩn là gì?
8. `kubectl get endpoints <svc>` trả `<none>` — nguyên nhân có thể là gì?
9. Tại sao phải test network từ trong cluster (pod tạm) thay vì từ laptop?
10. NetworkPolicy `deny-all-ingress` có chặn traffic giữa các pod trong cùng namespace không? Làm sao xác nhận?

### Đáp án

1. `kubectl describe <resource>` (Events) → `kubectl logs [--previous]` (app log) → `kubectl get events --sort-by=.lastTimestamp` (toàn namespace). Thứ tự này đi từ triệu chứng chung → nguyên nhân cụ thể.
2. `CrashLoopBackOff` = container đã crash nhiều lần, K8s tăng delay back-off trước mỗi lần retry. `Error` = container crash nhưng chưa vào vòng back-off. `--previous` cần vì sau khi restart, container mới lên với log trắng — `--previous` đọc log của container cũ (lần crash trước).
3. Readiness probe đang fail — Pod không sẵn sàng. Hậu quả: K8s gỡ Pod khỏi Endpoints, Service không route traffic đến Pod đó dù Pod đang Running.
4. `sudo systemctl status kubelet` (xem process còn sống không) → `sudo journalctl -u kubelet -n 50 --no-pager` (đọc log tìm nguyên nhân crash).
5. Kiểm tra thời gian hết hạn của tất cả cert K8s (apiserver, etcd, kubelet client, front-proxy…). Chạy trên **control-plane node** vì cert nằm tại `/etc/kubernetes/pki/`.
6. Static pod là pod được kubelet tự quản lý từ file manifest trong `/etc/kubernetes/manifests/` — không cần API server. Khi apiserver chết, kubelet vẫn đọc được manifest và có thể tự dựng lại apiserver; đây là lý do fix control-plane failure bằng cách sửa file manifest rồi để kubelet tự xử lý.
7. SSH vào control-plane node → `sudo crictl ps -a | grep apiserver` để xem container apiserver còn chạy không và lý do nếu Exited.
8. Selector trong Service không match label nào của Pod đang chạy (sai key hoặc sai value). Cũng có thể do không có Pod nào tồn tại với label đó.
9. ClusterIP không routable từ ngoài cluster — chỉ accessible trong cluster network. Test từ laptop sẽ luôn thất bại dù cấu hình đúng. Pod tạm nằm trong cluster network nên có thể dùng ClusterIP/DNS như workload thật.
10. Có — NetworkPolicy `policyTypes: Ingress` + `podSelector: app=backend` chặn **tất cả ingress** đến pod `app=backend` từ mọi nguồn kể cả pod cùng namespace (trừ khi có rule `from` cho phép). Xác nhận bằng `kubectl run net-test --image=busybox ... -- wget http://backend-svc` → timeout = bị chặn.

---

## Bắc cầu sang production

Troubleshooting K8s trên cụm thật không khác lab về mặt công cụ — chỉ khác ở quy mô (hàng trăm pod, nhiều namespace) và áp lực thời gian. Những gì nên luôn nhớ:

Chẩn theo tầng, không đoán mò. Triệu chứng → tầng nghi ngờ → lệnh cụ thể. Bỏ qua bước đọc Events và `logs --previous` là nguyên nhân số một khiến một sự cố kéo dài hàng giờ thay vì 5 phút.

`logs --previous` và `get events` là 2 lệnh đầu tiên — luôn. Phần lớn lỗi production (app crash, config sai, image pull fail) tự giải thích trong 2 lệnh này.

Node NotReady và control-plane failure yêu cầu SSH. `kubectl` vô dụng khi kubelet hoặc apiserver chết — cần biết địa chỉ SSH của node và có quyền `sudo`.

Troubleshooting là kỹ năng luyện bằng reps. Không có shortcut — càng phá nhiều, càng nhận diện pattern nhanh. Lab này chỉ là điểm khởi đầu; mỗi lần gặp sự cố thật trên cụm là một lần luyện tập quý giá hơn.

---

## 📎 Nguồn & xem lại

- [course-catalog](../../wiki/notebook/k8s/course-catalog.md) — bản đồ toàn chương trình, vị trí lab này trong lộ trình.
- [kubernetes.io/docs — Troubleshooting](https://kubernetes.io/docs/tasks/debug/) — tài liệu chính thức: debug pod, debug service, debug cluster.
- [kubernetes.io/docs — Application Introspection and Debugging](https://kubernetes.io/docs/tasks/debug/debug-application/)
- [kubernetes.io/docs — Debugging DNS Resolution](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/)
- [kubernetes.io/docs — Certificate Management with kubeadm](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/)
