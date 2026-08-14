# 15 · kubeadm — dựng cụm HA từ số 0 (3 master + 3 worker)

> **Chặng 7 · ◻ chưa mở** — [◈ Bảng tiến độ](../../wiki/notebook/k8s/sessions/learning-plan.md) · trước: RBAC & Security · kế tiếp: etcd backup & restore · [course-catalog](../../wiki/notebook/k8s/course-catalog.md)

**Mục tiêu:** hiểu kiến trúc HA stacked-etcd; dựng cụm 6 node với kubeadm; thao tác control-plane-endpoint + VIP; join thêm master và worker; verify etcd 3-member healthy.

**Nền:** đã biết Pod, Deployment, Service, RBAC (lab 3–14). Lab này chuyển sang admin-mode: không dùng cụm có sẵn — tự dựng từ đầu. Đây là arena cho toàn nhánh CKA-admin (15–18).

> ⚠ **Lưu ý phần cứng (Mac Mini M4, 24 GB) — chạy thật thế nào:**
> Dựng bằng **multipass** (giống lab `02-docker-swarm`). K8s nặng hơn Swarm nên đặt RAM/node cao hơn mức 2G Swarm dùng, nhưng vẫn vừa máy:
> - **HA đầy đủ 6 node** (bài này): mỗi VM **2 GB** → tổng **~12 GB**, vừa 24 GB (chừa ~12 GB cho macOS). Master 2 GB là **sàn của kubeadm** (preflight ≥ 1700 MB) — hơi sát; đóng app nặng + `multipass stop` các VM lab khác trước khi launch.
> - **Gọn khi RAM eo hẹp** (khuyến nghị nếu máy còn ít trống): **1 master + 2 worker** × 2 GB = ~6 GB — học trọn cơ chế init → join → CNI → verify, chỉ **bỏ phần quorum 3-master** (vẫn đọc mục 1 để hiểu; dựng đủ 3 master khi có máy RAM lớn hơn). Sửa 2 vòng `for` bên dưới thành `for n in m1` và `for n in w1 w2`.
> - Bảng chọn số node theo RAM: xem [`../02-docker-swarm/runbook.md`](../02-docker-swarm/runbook.md).
>
> **Output trong lab là MẪU chuẩn theo hành vi kubeadm — CHƯA chạy trên máy bạn; verify lại IP/số thật khi dựng.**

---

## Tiền đề
### 1 · Dựng 6 VM multipass

```bash
# (trên host) tạo 3 master
for n in m1 m2 m3; do
  multipass launch --name $n --cpus 2 --memory 2G --disk 20G 22.04
done

# tạo 3 worker
for n in w1 w2 w3; do
  multipass launch --name $n --cpus 2 --memory 2G --disk 20G 22.04
done

# kiểm tra 6 VM đang Running
multipass list
```

```text
Name       State    IPv4              Image
m1         Running  192.168.56.11     Ubuntu 22.04 LTS
m2         Running  192.168.56.12     Ubuntu 22.04 LTS
m3         Running  192.168.56.13     Ubuntu 22.04 LTS
w1         Running  192.168.56.21     Ubuntu 22.04 LTS
w2         Running  192.168.56.22     Ubuntu 22.04 LTS
w3         Running  192.168.56.23     Ubuntu 22.04 LTS
```

### 2 · Cài containerd + kubeadm/kubelet/kubectl (mọi node)

Chạy script sau trên **mỗi** trong 6 VM. Ví dụ với m1:

```bash
multipass shell m1
```

Bên trong VM — chạy đoạn này một lần:

```bash
# tắt swap (kubeadm yêu cầu)
sudo swapoff -a
sudo sed -i '/swap/d' /etc/fstab

# kernel modules + sysctl cho CNI
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF
sudo modprobe overlay
sudo modprobe br_netfilter

cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sudo sysctl --system

# cài containerd
sudo apt-get update -y && sudo apt-get install -y containerd
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml
# bật SystemdCgroup — bắt buộc với kubeadm
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd && sudo systemctl enable containerd

# cài kubeadm kubelet kubectl (phiên bản 1.30)
sudo apt-get install -y apt-transport-https ca-certificates curl gpg
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] \
  https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
sudo apt-get update -y
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
sudo systemctl enable kubelet
```

Lặp lại cho m2, m3, w1, w2, w3 (hoặc dùng `multipass exec <node> -- bash -c "..."` để không phải shell từng cái).

---

## 1. Kiến trúc HA — vì sao 3 master

**Chốt:** Kubernetes HA yêu cầu **số lẻ** control-plane node (3, 5, 7) vì etcd dùng **Raft consensus** — quorum = `floor(N/2)+1`. Với 3 node: quorum = 2, **chịu mất 1 node** mà cụm vẫn hoạt động. Mọi client (kubectl, worker kubelet) chỉ nói chuyện với **một VIP duy nhất** — không hardcode IP của master nào.

- **etcd Raft quorum:** N=3 → quorum=2 → chịu mất 1 member (còn 2 ≥ quorum).
- **Số lẻ là bắt buộc:** N=4 → quorum=3 → chịu mất 1 member — *cùng fault tolerance* với N=3 nhưng tốn thêm 1 node. Do đó N=4 là lãng phí; 3 → 5 khi muốn tăng tolerance.
- **Stacked etcd:** etcd chạy *trong* từng master (Pod tĩnh), cùng node với kube-apiserver. Ngược lại với *external etcd* (chạy trên VM riêng) — stacked đơn giản hơn, phù hợp lab và cụm không quá lớn.
- **control-plane-endpoint = VIP:** tất cả join command và `~/.kube/config` trỏ về 1 VIP (ví dụ `192.168.56.100:6443`). Khi 1 master chết, VIP di chuyển sang master khác (keepalived/kube-vip) — client không cần đổi config.

**Vì sao:** nếu hardcode IP của `m1` vào kubeconfig, khi `m1` chết toàn bộ kubectl và worker mất kết nối dù `m2`, `m3` vẫn sống. VIP + load balancer là lớp trừu tượng giúp control-plane thực sự HA.

**Cơ chế:** mỗi master chạy 4 Pod tĩnh (`kube-apiserver`, `kube-scheduler`, `kube-controller-manager`, `etcd`) do kubelet quản. etcd trên 3 node bầu leader qua Raft; write phải qua leader, read từ bất kỳ follower. kube-apiserver trên mỗi master kết nối etcd local — không qua mạng ngoài.

> 💡 **Ẩn dụ:** 3 master = ban giám đốc 3 người, phải có ≥2 người đồng ý mới ra quyết định (quorum). VIP = số điện thoại tổng đài — gọi vào đó, tổng đài chuyển sang bất kỳ giám đốc nào đang trực.

| N master | Quorum | Fault tolerance | Ghi chú |
|---|---|---|---|
| 1 | 1 | 0 | Không HA |
| 3 | 2 | 1 | **Tối thiểu HA** |
| 5 | 3 | 2 | Dùng khi cần chịu mất 2 |
| 7 | 4 | 3 | Cluster lớn, hiếm |

**Dùng/KHÔNG:**
- Dùng 3 master: mọi cụm production cần HA.
- KHÔNG dùng 2 hoặc 4 master: 2 node không có quorum khi 1 chết; 4 node tốn thêm mà fault tolerance bằng 3.
- **Phản đề:** stacked etcd gộp etcd và apiserver cùng node — nếu node bị áp lực CPU/RAM, etcd bị ảnh hưởng. Môi trường production nặng cần external etcd để cô lập tải.

**Làm:**
```bash
# (trên host) xem IP của 6 VM vừa tạo
multipass list
```

```text
Name       State    IPv4              Image
m1         Running  192.168.56.11     Ubuntu 22.04 LTS
m2         Running  192.168.56.12     Ubuntu 22.04 LTS
m3         Running  192.168.56.13     Ubuntu 22.04 LTS
w1         Running  192.168.56.21     Ubuntu 22.04 LTS
w2         Running  192.168.56.22     Ubuntu 22.04 LTS
w3         Running  192.168.56.23     Ubuntu 22.04 LTS
```

→ **Verify:** 6 dòng State=Running. Ghi lại IP của 3 master — dùng trong bước cấu hình VIP.

![[kubeadm-ha-topology.excalidraw]]

---

## 2. control-plane-endpoint + VIP

**Chốt:** trước khi `kubeadm init`, cần **VIP (Virtual IP) sẵn sàng trên ít nhất một master** — vì output `kubeadm init` sẽ ghi VIP vào certificate và kubeconfig. Nếu khởi động sau thì cert sai. Dùng **kube-vip** (DaemonSet static Pod) hoặc keepalived để cấp VIP.

- `--control-plane-endpoint=<VIP>:6443` truyền vào `kubeadm init`: địa chỉ này ghi vào SAN của cert apiserver và vào `~/.kube/config` — thay đổi sau rất khó.
- VIP là floating IP: kube-vip bầu leader trong 3 master, master nào đang là leader thì giữ IP `192.168.56.100` trên network interface. Master chết → leader mới lên → VIP di chuyển trong vài giây.
- **Port 6443:** mặc định của kube-apiserver; VIP expose port này → worker và kubectl reach.

**Vì sao:** `kubeadm init` sinh certificate với SAN (Subject Alternative Name) gồm VIP. Nếu cert không có SAN của VIP, kết nối TLS từ client đến VIP sẽ fail (certificate không khớp). Phải có VIP *trước* init, không thể thêm SAN sau mà không renew cert.

**Cơ chế:** kube-vip chạy như static Pod trên mỗi master (kubelet tự quản, không phụ thuộc API server). Nó dùng ARP (Layer 2) hoặc BGP (Layer 3) để broadcast VIP. Trong lab multipass (L2 network), dùng ARP mode. Khi master leader chết, kube-vip trên master còn lại giành VIP qua ARP announcement trong <2 giây.

> 💡 **Ẩn dụ:** VIP = biển số xe tổng đài — xe nào (master nào) đang chạy thì gắn biển đó. Xe hỏng → cơ quan rút biển sang xe khác, cuộc gọi vẫn đến đúng số.

| Giải pháp VIP | Cơ chế | Khi nào dùng |
|---|---|---|
| kube-vip | ARP/BGP, native K8s static Pod | Lab, cụm on-prem đơn giản |
| keepalived + HAProxy | VRRP + L4 LB | On-prem truyền thống |
| Cloud LB (AWS NLB, GCP LB) | Managed | Cloud deployment |

**Dùng/KHÔNG:**
- Dùng kube-vip cho lab và cụm on-prem L2 network đơn giản: ít thành phần, dễ debug.
- KHÔNG dùng kube-vip ARP mode trên mạng BGP/L3 routed — phải chuyển sang BGP mode hoặc external LB.
- **Phản đề:** VIP đơn điểm trong ARP mode — nếu master leader mất và kube-vip chưa failover xong (~2 giây), có thời gian ngắn apiserver không reachable. Môi trường zero-downtime dùng cloud LB có health check thật.

**Làm:**

```bash
# (trong m1) tạo manifest kube-vip static Pod
# VIP: 192.168.56.100, interface eth0 (kiểm tra bằng 'ip a' nếu tên khác)
KVVERSION=v0.8.0
INTERFACE=eth0
VIP=192.168.56.100

sudo mkdir -p /etc/kubernetes/manifests

sudo docker run --network host --rm \
  ghcr.io/kube-vip/kube-vip:${KVVERSION} \
  manifest pod \
  --interface $INTERFACE \
  --address $VIP \
  --controlplane \
  --arp \
  --leaderElection \
  | sudo tee /etc/kubernetes/manifests/kube-vip.yaml
```

Nếu chưa có docker trong VM, dùng `ctr` (containerd CLI) hoặc copy manifest mẫu:

```bash
# cách đơn giản hơn: dùng alias image để sinh manifest
sudo apt-get install -y jq
curl -sL https://raw.githubusercontent.com/kube-vip/kube-vip/main/docs/manifests/rbac.yaml \
  | sudo tee /etc/kubernetes/manifests/kube-vip-rbac.yaml

# verify file manifest tồn tại
ls -la /etc/kubernetes/manifests/kube-vip.yaml
```

```text
-rw-r--r-- 1 root root 2134 Aug 13 08:10 /etc/kubernetes/manifests/kube-vip.yaml
```

→ **Verify:** file `kube-vip.yaml` tồn tại trong `/etc/kubernetes/manifests/`. Sau khi `kubeadm init` xong, kubelet sẽ tự chạy Pod này và VIP `192.168.56.100` sẽ xuất hiện trên m1 (`ip a | grep 192.168.56.100`).

---

## 3. kubeadm init — bootstrap master đầu tiên

**Chốt:** `kubeadm init` trên m1 là lệnh bootstrap cụm — nó tạo CA, sinh cert, cấu hình etcd, chạy static Pod cho 4 thành phần control-plane, và in ra join command (có `--certificate-key` cho master, không có cho worker). Phải chạy **một lần duy nhất** trên **một** master.

- `--control-plane-endpoint`: VIP + port, ghi vào cert và kubeconfig.
- `--upload-certs`: mã hóa certificate và lưu vào secret `kubeadm-certs` trong kube-system — master 2, 3 dùng `--certificate-key` để download về. Tự hết hạn sau 2 giờ.
- `--pod-network-cidr`: CIDR cho Pod IP — phải chọn trước khi cài CNI (Calico mặc định `192.168.0.0/16`, Cilium `10.0.0.0/8`).
- `--apiserver-advertise-address`: IP của m1 dùng cho apiserver local — khác với VIP.

**Vì sao:** `kubeadm init` là một lần: nếu fail giữa chừng phải `kubeadm reset` và chạy lại. Certificate ghi vào cert — sai VIP hay sai CIDR ở bước này sẽ đau đớn sau. Đọc kỹ output trước khi join bất cứ node nào.

**Cơ chế:** kubeadm gọi theo thứ tự: (1) pre-flight check (swap, port, kernel param); (2) tạo CA và cert trong `/etc/kubernetes/pki/`; (3) viết kubeconfig vào `/etc/kubernetes/`; (4) viết static Pod manifest → kubelet tự khởi động etcd + apiserver + scheduler + controller-manager; (5) tạo ConfigMap `cluster-info` và RBAC bootstrap; (6) in join command.

> 💡 **Ẩn dụ:** `kubeadm init` = mở văn phòng mới — in con dấu (CA), cấp thẻ nhân viên (cert), kê bàn ghế (static Pod), rồi đưa hướng dẫn cho nhân viên mới join (join command).

| Flag kubeadm init | Mục đích | Giá trị trong lab |
|---|---|---|
| `--control-plane-endpoint` | VIP:port, ghi vào cert | `192.168.56.100:6443` |
| `--upload-certs` | Upload cert lên secret cho master 2,3 | (flag, không có value) |
| `--pod-network-cidr` | CIDR cho Pod — phải khớp CNI | `192.168.0.0/16` (Calico) |
| `--apiserver-advertise-address` | IP local của node đang init | `192.168.56.11` (IP m1) |

**Dùng/KHÔNG:**
- Dùng `--upload-certs` khi join thêm master: nếu không dùng, phải copy tay file cert sang m2/m3.
- KHÔNG thay đổi `--pod-network-cidr` sau khi init — thay đổi yêu cầu dựng lại cụm.
- **Phản đề:** `--upload-certs` tạo secret chứa private key — secret tự xóa sau 2 giờ. Nếu join m2/m3 sau 2 giờ phải chạy `kubeadm init phase upload-certs --upload-certs` để lấy certificate-key mới.

**Làm:**

```bash
# (trong m1) chạy kubeadm init
sudo kubeadm init \
  --control-plane-endpoint "192.168.56.100:6443" \
  --upload-certs \
  --pod-network-cidr "192.168.0.0/16" \
  --apiserver-advertise-address "192.168.56.11"
```

**Kết quả:**

```text
[init] Using Kubernetes version: v1.30.0
[preflight] Running pre-flight checks
[preflight] Pulling images required for setting up a Kubernetes cluster
[preflight] This might take a minute or two, depending on the speed of your internet connection
[certs] Using certificateDir folder "/etc/kubernetes/pki"
[certs] Generating "ca" certificate and key
[certs] Generating "apiserver" certificate and key
[certs] apiserver serving cert is signed for DNS names [kubernetes kubernetes.default
kubernetes.default.svc kubernetes.default.svc.cluster.local m1] and IPs
[192.168.56.11 10.96.0.1 192.168.56.100]
[certs] Generating "etcd/ca" certificate and key
[certs] Generating "etcd/server" certificate and key
[certs] Generating "etcd/peer" certificate and key
...
[bootstraptoken] configured RBAC rules to allow certificate rotation for each node identity
[addons] Applied essential addon: CoreDNS
[addons] Applied essential addon: kube-proxy

Your Kubernetes control-plane has initialized successfully!

To start using your cluster, you need to run the following as a regular user:

  mkdir -p $HOME/.kube
  sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
  sudo chown $(id -u):$(id -g) $HOME/.kube/config

You can now join any number of the control-plane node by running the following command on
each as root:

  kubeadm join 192.168.56.100:6443 --token abcdef.0123456789abcdef \
        --discovery-token-ca-cert-hash sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef \
        --control-plane --certificate-key 9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b

Please note that the certificate-key gives access to cluster sensitive data, keep it secret!
As a safeguard, uploaded-certs will be deleted in two hours; If necessary, you can use
"kubeadm init phase upload-certs --upload-certs" to reload certs afterward.

Then you can join any number of worker nodes by running the following on each as root:

kubeadm join 192.168.56.100:6443 --token abcdef.0123456789abcdef \
        --discovery-token-ca-cert-hash sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
```

Sau đó cấu hình kubeconfig cho user thường:

```bash
mkdir -p $HOME/.kube
sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

→ **Verify:** dòng `Your Kubernetes control-plane has initialized successfully!` xuất hiện. Lưu lại 2 join command: một dùng `--control-plane --certificate-key` (cho m2, m3) và một không có flag đó (cho w1, w2, w3). Kiểm tra thêm:

```bash
kubectl get nodes
```

```text
NAME   STATUS     ROLES           AGE   VERSION
m1     NotReady   control-plane   90s   v1.30.0
```

`NotReady` là bình thường — cluster chưa có CNI, node sẽ Ready sau bước 4.

---

## 4. CNI — cài pod network

**Chốt:** cluster ở trạng thái `NotReady` cho đến khi có **CNI plugin** — đây là thành phần cấp IP cho Pod và định tuyến traffic giữa các node. Không có CNI → Pod không có IP → CoreDNS không lên → cluster không dùng được.

- **CNI (Container Network Interface):** chuẩn plugin network cho K8s. kubelet gọi CNI khi tạo/xóa Pod để cấp/thu hồi IP.
- **Calico:** CNI phổ biến cho on-prem/self-managed; hỗ trợ NetworkPolicy; hoạt động ở L3 (BGP hoặc IPIP overlay). `--pod-network-cidr=192.168.0.0/16` khớp với Calico mặc định.
- Sau khi apply CNI manifest, `calico-node` DaemonSet khởi động trên mọi node → node chuyển sang `Ready` → CoreDNS Pod từ `Pending` → `Running`.

**Vì sao:** K8s cố tình không bundle CNI — để người vận hành chọn Calico, Cilium, Flannel,... theo nhu cầu (NetworkPolicy, eBPF, performance). Tách biệt này cho phép upgrade CNI độc lập với cluster.

**Cơ chế:** `kubectl apply` Calico manifest tạo DaemonSet `calico-node` + Deployment `calico-kube-controllers`. `calico-node` trên mỗi node gọi CNI binary `/opt/cni/bin/calico` khi kubelet tạo Pod. CNI binary cấp IP từ `pod-network-cidr`, tạo veth pair, cấu hình route. kubelet cập nhật node condition `NetworkReady=True` → node thành `Ready`.

> 💡 **Ẩn dụ:** cluster không CNI = tòa nhà không hệ thống điện — phòng (Pod) xây xong nhưng không có đèn, không dùng được. CNI = thợ điện kéo dây và cấp ổ cắm cho từng phòng.

| CNI | Điểm mạnh | Pod CIDR mặc định |
|---|---|---|
| Calico | NetworkPolicy, BGP, production | `192.168.0.0/16` |
| Cilium | eBPF, observability, hiệu năng cao | `10.0.0.0/8` |
| Flannel | Đơn giản, nhẹ | `10.244.0.0/16` |
| Weave | Đơn giản, mesh | `10.32.0.0/12` |

**Dùng/KHÔNG:**
- Dùng Calico cho lab CKA và cụm on-prem: tài liệu nhiều, tương thích tốt với kubeadm.
- KHÔNG mix CIDR: `--pod-network-cidr` khi init phải khớp với CIDR trong CNI manifest.
- **Phản đề:** Calico dùng BGP peer giữa các node — nếu firewall chặn port 179 (BGP) hoặc protocol 4 (IPIP), pod-to-pod traffic sẽ fail. Môi trường hạn chế port dùng Flannel (VXLAN) hoặc Cilium (GENEVE) đơn giản hơn.

**Làm:**

```bash
# (trong m1) cài Calico CNI
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.27.0/manifests/calico.yaml
```

```text
poddisruptionbudget.policy/calico-kube-controllers created
serviceaccount/calico-kube-controllers created
serviceaccount/calico-node created
configmap/calico-config created
customresourcedefinition.apiextensions.k8s.io/bgpconfigurations.crd.projectcalico.org created
...
daemonset.apps/calico-node created
deployment.apps/calico-kube-controllers created
```

Đợi các Pod CNI lên Running (~2 phút):

```bash
kubectl -n kube-system get pods -l k8s-app=calico-node -w
```

```text
NAME                READY   STATUS    RESTARTS   AGE
calico-node-x4t7p   0/1     Init:0/3  0          30s
calico-node-x4t7p   0/1     PodInitializing 0    90s
calico-node-x4t7p   1/1     Running   0          105s
```

Kiểm tra CoreDNS và node:

```bash
kubectl -n kube-system get pods | grep coredns
kubectl get nodes
```

```text
coredns-5d78c9869d-8hn4x   1/1     Running   0   4m
coredns-5d78c9869d-kp9xs   1/1     Running   0   4m

NAME   STATUS   ROLES           AGE     VERSION
m1     Ready    control-plane   5m30s   v1.30.0
```

→ **Verify:** `calico-node` STATUS=Running; CoreDNS 2 Pod Running; `m1` STATUS=`Ready`. Cluster sẵn sàng nhận thêm node.

---

## 5. Join thêm control-plane + worker

**Chốt:** join m2, m3 với flag `--control-plane --certificate-key` — kubeadm download cert từ secret, dựng etcd member thứ 2 và 3, chạy 4 static Pod. Join w1, w2, w3 không có `--control-plane` — chỉ register node vào cluster, chạy kubelet + kube-proxy.

- Join master: `kubeadm join <VIP>:6443 --token <token> --discovery-token-ca-cert-hash sha256:<hash> --control-plane --certificate-key <key>` + `--apiserver-advertise-address <IP của node đó>`.
- Join worker: `kubeadm join <VIP>:6443 --token <token> --discovery-token-ca-cert-hash sha256:<hash>` (không có `--control-plane`).
- **stacked etcd tự mở rộng:** khi m2 join, kubeadm gọi etcd API để add member — etcd cluster tự sync data từ leader. Không cần tay thêm member vào etcd.
- Token mặc định hết hạn sau 24 giờ. Tạo token mới: `kubeadm token create --print-join-command`.

**Vì sao:** join command chứa bootstrap token (tạm thời) để worker/master đầu tiên authenticate với API server. Sau khi join thành công, kubelet được cấp certificate dài hạn từ CA — token không còn cần. `--certificate-key` chỉ dùng khi join control-plane, để download cert CA đã mã hóa từ secret.

**Cơ chế:** `kubeadm join` phía worker: (1) connect API server qua VIP; (2) authenticate bằng token; (3) xác thực CA hash; (4) gửi CSR (Certificate Signing Request) để xin cert kubelet; (5) API server approve CSR; (6) kubelet nhận cert, khởi động, register node. Phía master thêm: download cert từ secret, dựng static Pod, thêm etcd member, ghi kubeconfig local.

> 💡 **Ẩn dụ:** join = nhân viên mới đến văn phòng bằng thẻ tạm (token), qua bảo vệ kiểm tra (CA hash), làm thủ tục nhân sự (CSR/cert), rồi nhận thẻ dài hạn (kubelet cert). Nhân viên cấp manager (master) còn nhận thêm chìa khóa kho bí mật (certificate-key).

| Bước join | Flag bắt buộc | Chỉ cho |
|---|---|---|
| `--token` | bootstrap token | mọi node |
| `--discovery-token-ca-cert-hash` | fingerprint CA | mọi node |
| `--control-plane` | đây là master | master thêm |
| `--certificate-key` | key để download cert | master thêm |
| `--apiserver-advertise-address` | IP local của node | master thêm |

**Dùng/KHÔNG:**
- Dùng `--apiserver-advertise-address` khi join master: không truyền thì kubeadm dùng default gateway interface, có thể chọn sai IP.
- KHÔNG dùng join command của worker để join master: sẽ join thành worker, không có etcd, không có static Pod control-plane.
- **Phản đề:** `--upload-certs` tạo secret tồn tại 2 giờ — join m2/m3 muộn hơn thì `--certificate-key` không còn hợp lệ. Giải pháp: `kubeadm init phase upload-certs --upload-certs` trên m1 để lấy certificate-key mới.

**Làm:**

Join m2 (master thứ 2):

```bash
# (trong m2) — thay token, hash, certificate-key bằng giá trị từ output kubeadm init
sudo kubeadm join 192.168.56.100:6443 \
  --token abcdef.0123456789abcdef \
  --discovery-token-ca-cert-hash sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef \
  --control-plane \
  --certificate-key 9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b \
  --apiserver-advertise-address 192.168.56.12
```

```text
[preflight] Running pre-flight checks
[preflight] Reading configuration from the cluster...
[certs] Using certificateDir folder "/etc/kubernetes/pki"
[certs] Generating "etcd/server" certificate and key
[certs] Generating "etcd/peer" certificate and key
...
[etcd] Announced new etcd member joining to existing etcd cluster
[control-plane] Creating static Pod files in "/etc/kubernetes/manifests"
...
This node has joined the cluster as a control-plane node.

To start administering your cluster from this node, you need to run the following as a
regular user:

        mkdir -p $HOME/.kube
        sudo cp -i /etc/kubernetes/admin.conf $HOME/.kube/config
        sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

Lặp tương tự cho m3 (thay `--apiserver-advertise-address 192.168.56.13`).

Join w1, w2, w3 (worker):

```bash
# (trong w1, w2, w3 — lần lượt) dùng join command worker (không có --control-plane)
sudo kubeadm join 192.168.56.100:6443 \
  --token abcdef.0123456789abcdef \
  --discovery-token-ca-cert-hash sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
```

```text
[preflight] Running pre-flight checks
[preflight] Reading configuration from the cluster...
[kubelet-start] Writing kubelet configuration to file "/var/lib/kubelet/config.yaml"
[kubelet-start] Writing kubelet environment file with flags to file "/var/lib/kubelet/kubeadm-flags.env"
[kubelet-start] Starting the kubelet
[kubelet-start] Waiting for the kubelet to perform the TLS Bootstrap...

This node has joined the cluster:
* Certificate signing request was sent to apiserver and a response was received.
* The Kubelet was informed of the new secure connection details.

Run 'kubectl get nodes' on the control-plane to see this node join the cluster.
```

Cài kube-vip RBAC trên m2, m3 (copy manifest từ m1) và copy kube-vip.yaml sang m2, m3 để VIP có thể failover:

```bash
# (trên host) copy kube-vip manifest sang m2, m3
multipass transfer m1:/etc/kubernetes/manifests/kube-vip.yaml /tmp/kube-vip.yaml
multipass transfer /tmp/kube-vip.yaml m2:/tmp/kube-vip.yaml
multipass exec m2 -- sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
multipass transfer /tmp/kube-vip.yaml m3:/tmp/kube-vip.yaml
multipass exec m3 -- sudo cp /tmp/kube-vip.yaml /etc/kubernetes/manifests/kube-vip.yaml
```

→ **Verify:** dòng `This node has joined the cluster as a control-plane node.` trên m2, m3; `This node has joined the cluster:` trên w1, w2, w3. Kiểm tra trên m1:

```bash
kubectl get nodes
```

```text
NAME   STATUS   ROLES           AGE     VERSION
m1     Ready    control-plane   12m     v1.30.0
m2     Ready    control-plane   6m      v1.30.0
m3     Ready    control-plane   3m      v1.30.0
w1     Ready    <none>          90s     v1.30.0
w2     Ready    <none>          60s     v1.30.0
w3     Ready    <none>          30s     v1.30.0
```

---

## 6. Verify cụm HA

**Chốt:** cụm HA hợp lệ khi: 6 node `Ready` (3 control-plane + 3 worker), etcd 3-member đều `started`, taint `NoSchedule` trên control-plane node ngăn Pod thường không lên master.

- **6 node Ready:** `kubectl get nodes` — 3 `ROLES=control-plane`, 3 `ROLES=<none>`.
- **etcd 3-member:** `etcdctl member list` — 3 member, status `started`.
- **Control-plane taint:** mặc định kubeadm thêm taint `node-role.kubernetes.io/control-plane:NoSchedule` vào 3 master — Pod thường không schedule lên đây trừ khi có toleration.
- **kube-system Pods:** tất cả running — etcd, kube-apiserver, scheduler, controller-manager (3 replica mỗi loại), CoreDNS (2 Pod), kube-proxy (6 Pod — mỗi node 1 Pod).

**Vì sao:** một cụm có 6 node Ready không đồng nghĩa HA nếu etcd chỉ có 1 member, hoặc nếu VIP không failover. Verify etcd member list và thử `kubectl get nodes` qua từng master IP riêng để xác nhận apiserver sống trên cả 3.

**Cơ chế:** `etcdctl` là CLI của etcd; cần truyền cert để authenticate (etcd bật mTLS). Cert nằm trong `/etc/kubernetes/pki/etcd/`. `etcdctl endpoint health` kiểm tra từng endpoint có reachable và kv store healthy không.

> 💡 **Ẩn dụ:** verify HA = kiểm tra xe backup đề máy được, không chỉ kiểm tra xe chính đang chạy. etcd member list = đếm đúng 3 người trong ban giám đốc; endpoint health = mỗi người trả lời điện thoại.

| Lệnh verify | Kiểm tra gì |
|---|---|
| `kubectl get nodes` | 6 node Ready, role đúng |
| `kubectl -n kube-system get pods` | mọi Pod Running, không Pending/CrashLoop |
| `etcdctl member list` | 3 member, status=started |
| `etcdctl endpoint health` | mỗi endpoint isHealthy=true |
| `kubectl describe node m1` | taint NoSchedule trên control-plane |

**Dùng/KHÔNG:**
- Dùng `etcdctl endpoint health` định kỳ khi monitoring: nếu 1 member unhealthy, phải xử lý trước khi mất quorum.
- KHÔNG xóa taint control-plane trên production để chạy workload: tạo áp lực lên node cũng chạy apiserver/etcd → rủi ro stability.
- **Phản đề:** `kubectl get nodes` Ready không có nghĩa cụm 100% healthy — kubelet trên node có thể Ready nhưng etcd leader chưa ổn định. Luôn check `etcdctl endpoint health` sau khi join master mới.

**Làm:**

```bash
# (trên m1) — xem 6 node
kubectl get nodes -o wide
```

```text
NAME   STATUS   ROLES           AGE     VERSION   INTERNAL-IP      OS-IMAGE
m1     Ready    control-plane   15m     v1.30.0   192.168.56.11    Ubuntu 22.04.4 LTS
m2     Ready    control-plane   9m      v1.30.0   192.168.56.12    Ubuntu 22.04.4 LTS
m3     Ready    control-plane   6m      v1.30.0   192.168.56.13    Ubuntu 22.04.4 LTS
w1     Ready    <none>          3m30s   v1.30.0   192.168.56.21    Ubuntu 22.04.4 LTS
w2     Ready    <none>          3m      v1.30.0   192.168.56.22    Ubuntu 22.04.4 LTS
w3     Ready    <none>          2m30s   v1.30.0   192.168.56.23    Ubuntu 22.04.4 LTS
```

```bash
# xem tất cả Pod kube-system
kubectl -n kube-system get pods -o wide
```

```text
NAME                                      READY   STATUS    RESTARTS   AGE   NODE
calico-kube-controllers-77d59654f4-xtz9p  1/1     Running   0          13m   m1
calico-node-4bt7k                         1/1     Running   0          13m   m1
calico-node-8jnmq                         1/1     Running   0          8m    m2
calico-node-q9rsp                         1/1     Running   0          5m    m3
calico-node-rp2w7                         1/1     Running   0          3m    w1
calico-node-vm4ks                         1/1     Running   0          2m    w2
calico-node-x9hpq                         1/1     Running   0          2m    w3
coredns-5d78c9869d-8hn4x                  1/1     Running   0          15m   m1
coredns-5d78c9869d-kp9xs                  1/1     Running   0          15m   m3
etcd-m1                                   1/1     Running   0          15m   m1
etcd-m2                                   1/1     Running   0          9m    m2
etcd-m3                                   1/1     Running   0          6m    m3
kube-apiserver-m1                         1/1     Running   0          15m   m1
kube-apiserver-m2                         1/1     Running   0          9m    m2
kube-apiserver-m3                         1/1     Running   0          6m    m3
kube-controller-manager-m1               1/1     Running   0          15m   m1
kube-controller-manager-m2               1/1     Running   0          9m    m2
kube-controller-manager-m3               1/1     Running   0          6m    m3
kube-proxy-2kxqp                          1/1     Running   0          15m   m1
kube-proxy-5rsp8                          1/1     Running   0          9m    m2
kube-proxy-7l9kn                          1/1     Running   0          6m    m3
kube-proxy-b8vqm                          1/1     Running   0          3m    w1
kube-proxy-j2qzn                          1/1     Running   0          2m    w2
kube-proxy-nxh4p                          1/1     Running   0          2m    w3
kube-scheduler-m1                         1/1     Running   0          15m   m1
kube-scheduler-m2                         1/1     Running   0          9m    m2
kube-scheduler-m3                         1/1     Running   0          6m    m3
kube-vip-m1                               1/1     Running   0          15m   m1
kube-vip-m2                               1/1     Running   0          9m    m2
kube-vip-m3                               1/1     Running   0          6m    m3
```

Kiểm tra etcd 3-member:

```bash
# (trên m1) dùng etcdctl với cert của etcd
sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/peer.crt \
  --key=/etc/kubernetes/pki/etcd/peer.key \
  member list
```

```text
5c7a4e1b3f8d2a6c, started, m1, https://192.168.56.11:2380, https://192.168.56.11:2379, false
8a2b9d4e7f1c3e5d, started, m2, https://192.168.56.12:2380, https://192.168.56.12:2379, false
b3e4f7a1c9d2e5f8, started, m3, https://192.168.56.13:2380, https://192.168.56.13:2379, false
```

```bash
# health check
sudo ETCDCTL_API=3 etcdctl \
  --endpoints=https://192.168.56.11:2379,https://192.168.56.12:2379,https://192.168.56.13:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/peer.crt \
  --key=/etc/kubernetes/pki/etcd/peer.key \
  endpoint health
```

```text
https://192.168.56.11:2379 is healthy: successfully committed proposal: took = 6.3ms
https://192.168.56.12:2379 is healthy: successfully committed proposal: took = 7.1ms
https://192.168.56.13:2379 is healthy: successfully committed proposal: took = 8.4ms
```

Xem taint trên control-plane:

```bash
kubectl describe node m1 | grep Taint
```

```text
Taints:             node-role.kubernetes.io/control-plane:NoSchedule
```

→ **Verify:** `etcdctl member list` in 3 dòng status=`started`; `endpoint health` 3 endpoint đều `is healthy`; `kubectl get nodes` 6 dòng STATUS=`Ready`; 3 master có taint `NoSchedule`.

---

## 🧹 Dọn dẹp

```bash
# (trong từng node — m1, m2, m3, w1, w2, w3) reset kubeadm
sudo kubeadm reset -f
sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet /etc/cni/net.d
sudo iptables -F && sudo iptables -t nat -F

# (trên host) xóa VM multipass
multipass delete m1 m2 m3 w1 w2 w3
multipass purge
```

---

## ✅ Đủ khi

① giải thích được vì sao 3 master (không phải 2, 4) và etcd quorum là gì · ② nói được VIP phải sẵn sàng *trước* `kubeadm init` và tại sao (SAN cert) · ③ đọc được output `kubeadm init` và phân biệt 2 join command (master vs worker) · ④ giải thích cluster NotReady trước CNI và CNI làm gì để fix · ⑤ join master với đúng flag (`--control-plane --certificate-key --apiserver-advertise-address`) · ⑥ verify etcd 3-member bằng `etcdctl member list` và hiểu taint NoSchedule trên control-plane.

---

## 🧠 Recall

1. Vì sao cụm HA K8s cần số lẻ master (3, 5, 7)?
2. etcd quorum với N=3 là bao nhiêu? Chịu mất tối đa bao nhiêu node?
3. Sự khác nhau giữa stacked etcd và external etcd là gì?
4. Tại sao `control-plane-endpoint` phải là VIP, không phải IP của master cụ thể?
5. Tại sao VIP phải sẵn sàng *trước* khi chạy `kubeadm init`?
6. `--upload-certs` trong `kubeadm init` làm gì? Tự xóa sau bao lâu?
7. Cluster ở trạng thái `NotReady` sau `kubeadm init` — nguyên nhân là gì và cách fix?
8. Khi join master thêm (`m2`, `m3`), flag nào khác so với join worker?
9. Lệnh nào kiểm tra etcd 3-member đều healthy? Cần truyền gì?
10. Taint `node-role.kubernetes.io/control-plane:NoSchedule` có tác dụng gì? Khi nào tắt đi?

### Đáp án

1. etcd dùng Raft consensus — quorum = `floor(N/2)+1`. Số lẻ tối ưu hóa fault tolerance: N=3 chịu mất 1, N=4 chịu mất 1 (bằng N=3 nhưng tốn thêm 1 node). Số chẵn không tăng fault tolerance mà tăng chi phí.
2. N=3 → quorum=2. Chịu mất tối đa **1** node (còn 2 ≥ quorum để bầu leader và commit write).
3. Stacked: etcd chạy trên cùng node với master (Pod tĩnh, chia sẻ tài nguyên). External: etcd chạy trên VM/node riêng (cô lập tài nguyên, phức tạp hơn nhưng ổn định hơn khi tải cao).
4. Nếu dùng IP cụ thể của `m1`, khi `m1` chết toàn bộ client (kubectl, worker kubelet) mất kết nối dù `m2`, `m3` vẫn sống. VIP di chuyển sang master còn sống — client không cần đổi config.
5. `kubeadm init` ghi VIP vào SAN (Subject Alternative Name) của certificate apiserver. Nếu cert không có SAN của VIP, kết nối TLS đến VIP sẽ fail. Thêm SAN sau cần renew cert — phức tạp.
6. `--upload-certs` mã hóa certificate CA và lưu vào Secret `kubeadm-certs` trong `kube-system`. Master 2, 3 dùng `--certificate-key` để download. Secret **tự xóa sau 2 giờ** — join master muộn hơn cần chạy lại `kubeadm init phase upload-certs --upload-certs`.
7. Nguyên nhân: chưa có CNI — kubelet không thể cấp IP cho Pod, node condition `NetworkReady=False`. Cách fix: `kubectl apply` manifest CNI (Calico/Cilium/Flannel). Sau khi `calico-node` DaemonSet Running trên node → node chuyển `Ready`.
8. Join master thêm 3 flag: `--control-plane` (dựng static Pod + etcd member), `--certificate-key <key>` (download cert CA từ secret), `--apiserver-advertise-address <IP local của node đó>`.
9. `etcdctl endpoint health --endpoints=<IP1>:2379,<IP2>:2379,<IP3>:2379 --cacert=... --cert=... --key=...`. Cần truyền 3 cert: `cacert` (CA), `cert` (peer cert), `key` (peer key) vì etcd bật mTLS.
10. Ngăn Pod thường (không có toleration tương ứng) được schedule lên node control-plane. Bảo vệ tài nguyên cho etcd, apiserver, scheduler. Chỉ tắt đi (`kubectl taint nodes <node> node-role.kubernetes.io/control-plane:NoSchedule-`) khi cụm 1 node (dev) hoặc có lý do đặc biệt — không tắt trên production.

---

## Bắc cầu sang production

Cụm 3 master + stacked etcd là baseline HA: chịu mất 1 master mà cluster vẫn hoạt động bình thường — apiserver còn 2, etcd vẫn có quorum. Khi master hỏng, VIP failover sang master còn sống trong vài giây; kubectl và worker kubelet không cần đổi config.

Tuy nhiên: **etcd backup off-cluster vẫn bắt buộc** ngay cả khi đã HA. HA bảo vệ khỏi node failure, không bảo vệ khỏi data corruption hay accidental delete. Snapshot etcd ra storage ngoài (S3, NFS) là lớp an toàn thứ hai — đó là nội dung module 16.

Thực tế production thêm: external LB có health check (thay kube-vip cho môi trường nghiêm trọng hơn), external etcd cho cụm lớn nhiều tenant, dedicated etcd SSD để tách I/O.

---

## 📎 Nguồn & xem lại

- [course-catalog](../../wiki/notebook/k8s/course-catalog.md) — vị trí module này trong lộ trình CKA
- [kubernetes.io — kubeadm HA topology](https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/ha-topology/)
- [kubernetes.io — kubeadm init](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-init/)
- [kubernetes.io — kubeadm join](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-join/)
- [kube-vip docs](https://kube-vip.io/docs/)
