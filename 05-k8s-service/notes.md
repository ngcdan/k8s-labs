# Kubernetes — Service: địa chỉ ổn định cho Pod ephemeral

Bộ câu hỏi tự kiểm sau khi làm xong lab. Đọc câu hỏi, tự trả lời trong đầu, rồi mở phần đáp án để
đối chiếu. Các bước thực hành ở [k8s-service.md](k8s-service.md).

## Vì sao cần Service

<details>
<summary>1. Pod đổi IP khi nào? Vì sao không dùng IP Pod làm địa chỉ gọi?</summary>

Pod đổi IP khi: bị reschedule (chết → Pod mới IP mới), scale out (Pod mới = IP mới), hoặc mới schedule
(IP chỉ có sau khi đặt lên node). Lab thật: xóa `web-...-7j8jj` (IP `.24`) → `web-...-psjhl` mọc IP
`.25`. Client hard-code IP `.24` → gọi vào địa chỉ chết. Vì thế cần Service — endpoint ổn định
(ClusterIP + DNS) đứng trước tập Pod.
</details>

## Label selector & Endpoints

<details>
<summary>2. Service biết route tới Pod nào nhờ cơ chế gì?</summary>

**Label selector**, KHÔNG qua tên Pod. Service `spec.selector` (vd `app: web`) so với `metadata.labels`
của Pod; Pod khớp → IP đưa vào EndpointSlice. Tách khỏi tên Pod giúp Service hoạt động với Deployment
(Pod tên random) và Blue/Green (đổi selector, không đổi Service).
</details>

<details>
<summary>3. <code>get endpoints</code> / EndpointSlice thực chất là gì?</summary>

Một **object riêng do `endpoint-controller` tự sinh & duy trì** (không phải bạn tạo) — "bảng tra cứu
động" liệt kê IP:port các Pod khớp selector **và** đang Ready. Pod chết → gỡ IP; Pod mới Ready → thêm
IP. **kube-proxy đọc bảng này** để viết iptables/ipvs rule. `Endpoints = <none>` = selector sai label
→ Service có IP nhưng không route đi đâu (chẩn đoán số 1).
</details>

<details>
<summary>4. <code>Endpoints</code> vs <code>EndpointSlice</code> — khác gì, vì sao đổi? (thực chạy)</summary>

`Endpoints` (cũ, deprecated v1.33+) gom TẤT CẢ IP vào 1 object → phình to, gây tải etcd/API khi Service
có hàng nghìn Pod. `EndpointSlice` (mới) chia thành nhiều slice (~100 IP/slice) → scale tốt hơn. Lab
thật (v1.34): `kubectl get endpoints` in cảnh báo deprecated; xem bản mới bằng
`kubectl get endpointslice -l kubernetes.io/service-name=<svc>`.
</details>

## 4 loại Service

<details>
<summary>5. ClusterIP vs NodePort vs LoadBalancer — mỗi loại là gì, ai gọi được?</summary>

- **ClusterIP** (mặc định): IP ảo chỉ sống trong cluster (chỉ là rule iptables, không gắn NIC). Pod
 trong cluster gọi được, ngoài không.
- **NodePort** = ClusterIP + mở 1 cổng tĩnh (30000–32767) trên mọi node → gọi từ ngoài qua
 `<NodeIP>:<nodePort>`. Route: `NodeIP:nodePort → ClusterIP → PodIP`.
- **LoadBalancer** = NodePort + external IP thật (cloud/MetalLB cấp).
- **ExternalName** = alias DNS cho tên ngoài (vd `api.example.com`), không trỏ Pod.

Ba tầng chồng nhau: LoadBalancer ⊃ NodePort ⊃ ClusterIP.
</details>

<details>
<summary>6. (thực chạy) Trên OrbStack curl ClusterIP từ host lại được — vì sao? Có phổ quát không?</summary>

**Không phổ quát.** Trên cụm kind/cloud, ClusterIP KHÔNG gọi được từ host. OrbStack (giống Docker
Desktop) tự cắm route dải mạng cluster vào host để dev tiện — `netstat -rn | grep <subnet>` thấy route
qua `bridgeXXX`. Lab thật xác nhận: có route cho `192.168.194.0/24` và cả `.200` (ClusterIP web-svc)
qua `bridge102`. Đừng dựa vào; cách portable để lộ Service là NodePort/LoadBalancer.
</details>

## port vs targetPort

<details>
<summary>7. <code>port: 8080, targetPort: 80</code> nghĩa là gì? Chứng minh chúng tách rời thế nào?</summary>

Caller gọi Service tại cổng **8080** (`port`); kube-proxy DNAT sang cổng **80** của container
(`targetPort`) trong Pod. Lab thật: `curl <clusterIP>:8080` → nginx OK; `curl <clusterIP>:80` → treo
(Service không mở cổng 80). Dùng khi app nghe cổng lạ (Node.js 3000) nhưng muốn caller gọi cổng chuẩn
(80) — không phải sửa code app.
</details>

## DNS nội bộ

<details>
<summary>8. Pod A gọi Pod B mà không biết IP của B bằng cách nào?</summary>

Dùng **tên Service** làm hostname: `curl http://web-clusterip:8080`. CoreDNS resolve tên Service →
ClusterIP → kube-proxy route vào PodIP. Trong namespace khác: `<svc>.<namespace>.svc.cluster.local`.
Không bao giờ hardcode ClusterIP trong code (IP đổi nếu Service bị xóa/tạo lại).
</details>

## Scale (câu hỏi mở rộng)

<details>
<summary>9. <code>kubectl scale</code> áp được lên resource nào? Không áp được lên gì?</summary>

Áp được lên thứ có **scale subresource** (`replicas`): Deployment, ReplicaSet, StatefulSet,
ReplicationController. KHÔNG áp được: Pod trần (1 instance), DaemonSet (số bản = số node khớp), Job
(chỉnh `spec.parallelism`, không phải `scale`).
</details>

<details>
<summary>10. Scale lên vs scale xuống — khác gì bên trong?</summary>

Cùng reconcile `current` vs `desired`, khác hướng. **Lên** (`current < desired`): tạo Pod mới từ
template → pull image → readiness → thêm IP vào EndpointSlice. **Xuống** (`current > desired`): chọn Pod
theo thứ tự ưu tiên (chưa Ready → trẻ hơn → node đông Pod) → **gỡ IP khỏi EndpointSlice NGAY** → SIGTERM
→ chờ `terminationGracePeriodSeconds` (30s) → kill. Điểm tinh tế: scale xuống **gỡ endpoint trước, kill
sau** → không rớt request đang chạy (graceful). Xem sơ đồ `assets/scale-mechanism.excalidraw`.
</details>

## Nhiều resource trong 1 YAML

<details>
<summary>11. Khai báo Deployment/Service/... trong 1 file được không? Phân biệt & match thế nào?</summary>

Được — ngăn các document bằng `---`. **Phân biệt** resource: `apiVersion` + `kind` + `metadata.name`.
**Match** nhau qua **labels**, không qua tên: `Deployment.spec.selector.matchLabels` phải khớp
`spec.template.metadata.labels` (sai → lỗi `selector does not match template`); `Service.spec.selector`
khớp Pod labels → xây EndpointSlice. ReplicaSet do Deployment tự tạo (thêm `pod-template-hash`). Xem sơ
đồ `assets/yaml-multi-resource.excalidraw`.
</details>

## Bắc cầu sang Kubernetes production

<details>
<summary>12. Debug Service không route được — làm gì đầu tiên?</summary>

`kubectl get endpointslice -l kubernetes.io/service-name=<svc>` (hoặc `get endpoints`). Nếu `<none>` →
selector sai label → sửa `spec.selector` cho khớp Pod labels. Trên cụm thật: microservice gọi nhau qua
ClusterIP + tên DNS; traffic ngoài vào qua LoadBalancer (cloud/MetalLB cấp EXTERNAL-IP).

| Service (module này) | Kubernetes production |
|---|---|
| ClusterIP + DNS | microservice gọi nhau bằng tên |
| EndpointSlice tự cập nhật | traffic luôn tới Pod đang sống |
| NodePort/LoadBalancer | lộ traffic ra ngoài cluster |
| `<none>` endpoints = selector sai | bước debug đầu tiên |
</details>

## Ôn tập — đào sâu

<details>
<summary>13. Ai VIẾT EndpointSlice, ai ĐỌC nó? (đừng gộp controller với kube-proxy)</summary>

Đây là chỗ hay lẫn nhất. Có **3 object/thành phần khác nhau**, KHÔNG phải một:

```
Service (bạn tạo)  ──selector: app=web──►  EndpointSlice (tự sinh)  ──►  [Pod .24, Pod .25, ...]
   - có ClusterIP + selector                   - danh sách IP:port các Pod KHỚP selector VÀ Ready
```

| Thành phần | Vai trò | Làm gì với EndpointSlice |
|---|---|---|
| **endpointslice-controller** (trong kube-controller-manager) | **TẠO & cập nhật** | Quét Pod khớp selector + Ready → ghi IP vào slice; Pod chết → gỡ IP; Pod mới Ready → thêm IP |
| **kube-proxy** | **ĐỌC** | Đọc slice → dịch thành iptables/ipvs rule trên mỗi node để DNAT |

→ **controller VIẾT bảng, kube-proxy ĐỌC bảng, Service chỉ trỏ tới bảng.** EndpointSlice KHÔNG phải "object
service" và KHÔNG do kube-proxy tạo. iptables/ipvs rule là **sản phẩm của kube-proxy**, không phải nội dung
của EndpointSlice.
</details>

<details>
<summary>14. ClusterIP là IP "ma" — vậy <code>curl ClusterIP:port</code> chạy được nhờ đâu? (DNAT / iptables / ipvs cho người không rành mạng)</summary>

**ClusterIP là IP không gắn vào card mạng nào cả** — không máy/Pod nào thực sự "cầm" nó. Gói tin tới ClusterIP
chạy được là nhờ **kernel Linux chặn lại giữa đường và tráo địa chỉ đích** sang IP Pod thật.

**NAT = Network Address Translation** = đổi địa chỉ trên gói tin (như lá thư có địa-chỉ-gửi & địa-chỉ-nhận):
- **DNAT (Destination NAT)** = sửa **địa chỉ NHẬN** (đích). Đây là cái Service dùng.
- **SNAT (Source NAT)** = sửa địa chỉ GỬI (nguồn).

```
Trước DNAT:  [đích = 10.96.0.50:8080]   ← ClusterIP ma + port Service
Sau  DNAT:   [đích = 10.244.1.25:80 ]   ← IP Pod thật + targetPort container
```

DNAT vừa đổi **IP** (ClusterIP→PodIP) vừa đổi **port** (`port`→`targetPort`) — chính là chỗ `8080→80` xảy ra.

**iptables** và **ipvs** = 2 cơ chế CÓ SẴN trong kernel Linux (không phải của K8s) để lọc/định tuyến/biến đổi
gói tin. kube-proxy dùng một trong hai để **viết luật DNAT**. Coi cả hai là "sổ luật DNAT trong kernel";
iptables mặc định (cụm nhỏ/vừa), ipvs tối ưu cho cụm lớn hàng nghìn Service (iptables chậm dần khi luật quá
nhiều).

**Load balancing** đến từ đây: Service có 3 Pod → EndpointSlice liệt kê 3 IP → kube-proxy viết luật "bốc ngẫu
nhiên 1 trong 3 Pod IP để DNAT" → kernel tự cân tải, **không cần phần mềm load balancer riêng**.

Chuỗi trách nhiệm đầy đủ (khi gõ `curl 10.96.0.50:8080`):
1. **endpointslice-controller** quét Pod Ready → viết danh sách IP vào EndpointSlice.
2. **kube-proxy** đọc slice → viết luật DNAT vào iptables/ipvs của kernel mỗi node.
3. **kernel Linux** thực thi: gói tới ClusterIP → DNAT sang một Pod IP thật (cân tải ngẫu nhiên).

3 lớp: **controller biết-ai → kube-proxy dịch-thành-luật → kernel thực-thi.** ClusterIP "ma" hoạt động được
nhờ tầng kernel tráo địa chỉ, chứ bản thân IP đó không có thật.
</details>

