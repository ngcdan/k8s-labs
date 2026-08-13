# 06 · ConfigMap & Secret — tách cấu hình khỏi image, bơm vào Pod

> **Chặng 1** — trước: Service · kế tiếp: Volume/PV/PVC/StorageClass

**Mục tiêu:** hiểu vì sao phải tách config/secret khỏi image; biết tạo ConfigMap và Secret bằng nhiều cách; dùng được hai kỹ thuật tiêu thụ (env var và volume mount); nắm rõ điểm khác biệt quan trọng giữa hai kỹ thuật đó; và tránh bẫy "base64 không phải mã hoá".
**Nền:** đã biết Pod, Deployment, Service — ConfigMap/Secret là resource K8s thêm vào, Pod tham chiếu qua tên.
**⏱** 60–75 phút · **Sân:** host local (OrbStack Kubernetes).

> Mỗi mục: **Chốt → Vì sao → Cơ chế → Dùng/không → Làm → Kết quả** (output để đối chiếu). Đọc để *hiểu*, gõ để *thấy*.

## Tiền đề (1 lần)
```bash
kubectl config use-context orbstack
kubectl get nodes            # 1 node STATUS=Ready
```

---

## 1. Vì sao tách cấu hình khỏi image — nguyên tắc 12-Factor

**Chốt:** bake cứng config vào image → phải build lại mỗi khi đổi env; K8s schedule Pod bất kỳ node nào → không thể drop file lên host. ConfigMap và Secret giải quyết cả hai vấn đề.

- **Config thay đổi theo môi trường**: URL DB, API key, cờ feature, tên queue — dev ≠ staging ≠ prod.
- **Bake cứng vào image** → mỗi env phải build image riêng → vi phạm *"một image, nhiều môi trường"* (12-Factor App, factor III: config).
- **K8s schedule Pod** trên bất kỳ node nào và scale ra nhiều bản → không thể drop file lên host.
- **ConfigMap** (config không nhạy cảm) và **Secret** (nhạy cảm: password, token, cert) lưu tập trung trong cluster, inject vào container khi Pod khởi động.

**Vì sao:** giữ image bất biến → cùng image chạy ở dev/staging/prod, chỉ khác config — đây là nền tảng của CI/CD đáng tin cậy. Thay đổi config không cần build lại image, chỉ cần cập nhật ConfigMap/Secret rồi rolling-restart Deployment.

**Cơ chế:** ConfigMap và Secret là object K8s trong `etcd` trên control plane. Khi Pod được scheduled, kubelet trên node kéo các object này và inject vào container theo spec — qua biến môi trường (tại startup) hoặc volume mount (live, cập nhật được). K8s chỉ gửi Secret đến node nào đang có Pod cần nó → giảm attack surface.

> **Ẩn dụ:** image = khuôn đúc nhà (bất biến); ConfigMap/Secret = nội thất lắp vào sau — đổi nội thất không cần đúc lại khuôn.

**Dùng / KHÔNG:** ConfigMap cho mọi config không nhạy cảm; Secret cho credential, token, cert. **Phản đề:** với config cực đơn giản (1 env, không bao giờ đổi) thì overhead ConfigMap không đáng — có thể hardcode trong `spec.containers[].env[].value` trực tiếp; nhưng ngay khi có ≥2 env hoặc config có thể thay đổi → tách ra ConfigMap ngay.

**Làm:**
```bash
kubectl get configmaps      # hoặc: kubectl get cm
kubectl get secrets
```
**Kết quả:**
```text
$ kubectl get cm
NAME               DATA   AGE
kube-root-ca.crt   1      5d    ← system, không đụng tới

$ kubectl get secrets
NAME                  TYPE                DATA   AGE
                                                  ← rỗng nếu cluster mới
```
→ **Verify:** cluster sạch, chưa có ConfigMap/Secret do mình tạo.

---

## 2. Tạo ConfigMap

**Chốt:** bốn cách tạo ConfigMap, khác nhau ở nguồn đầu vào — chọn theo cách bạn có config sẵn; kết quả cuối đều là `data: key: value` trong etcd.

- **YAML manifest** — declarative, check vào git, thấy rõ cấu trúc.
- **`--from-env-file`** — mỗi dòng `KEY=value` → một cặp riêng; giống manifest nhất.
- **`--from-file`** — tên file = key, toàn bộ nội dung file = một blob value.
- **`--from-literal`** — nhanh cho dev/test, không nên dùng production (không track được).

**Vì sao:** hiểu sự khác biệt giữa `--from-file` và `--from-env-file` để không bị bất ngờ khi đọc `data`: `--from-file` tạo một key duy nhất chứa toàn bộ nội dung file (một blob), trong khi `--from-env-file` tạo nhiều key riêng biệt — cú pháp tham chiếu trong Pod khác nhau.

**Cơ chế:** `kubectl apply -f` / `kubectl create configmap` đều gọi API server ghi object vào etcd. Mỗi cặp `key: value` trong `data` tương ứng một biến env hoặc một file khi mount. Key phải là tên hợp lệ DNS label (nếu dùng làm env var) hoặc tên file (nếu mount volume).

> **Ẩn dụ:** ConfigMap = `.env` file được lưu trong cluster thay vì trên disk máy host — mọi Pod đều đọc từ cùng một nguồn, không mỗi máy một bản.

| Cách tạo | Kết quả `data` | Dùng khi |
|---|---|---|
| YAML manifest | nhiều key riêng, thấy rõ trong file | production, check git |
| `--from-env-file` | nhiều key riêng (giống manifest) | có sẵn file `.env` |
| `--from-file` | 1 key, blob toàn bộ file | config file nguyên khối (nginx.conf…) |
| `--from-literal` | nhiều key riêng | dev/test nhanh |

**Dùng / KHÔNG:** manifest cho production (review được, version được). **Phản đề:** `--from-literal` tiện nhưng không track được trong git → khi cluster chết bạn không biết giá trị cũ là gì. Ngoại lệ hợp lý: dev local thử nhanh trước khi viết YAML.

**Làm:**
```bash
# tạo file env
cat > /tmp/game-config.env <<'EOF'
ENEMIES=aliens
LIVES=3
CHEAT_LEVEL=noGoodRotten
EOF

# cách --from-env-file: mỗi dòng = một key
kubectl create configmap app-settings --from-env-file=/tmp/game-config.env
kubectl get cm app-settings -o yaml
kubectl delete cm app-settings

# cách --from-literal: gõ trực tiếp
kubectl create configmap app-settings \
  --from-literal=ENEMIES=aliens \
  --from-literal=LIVES=3
kubectl get cm app-settings -o yaml
```
**Kết quả — `kubectl get cm app-settings -o yaml` (cách `--from-env-file`):**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-settings
data:
  CHEAT_LEVEL: noGoodRotten   ← mỗi dòng env = một key riêng
  ENEMIES: aliens
  LIVES: "3"
```
→ **Verify:** `data` có 3 key riêng biệt (không phải một blob). So sánh: nếu dùng `--from-file=/tmp/game-config.env` thì `data` chỉ có 1 key tên `game-config.env` chứa toàn bộ nội dung.

---

## 3. Dùng ConfigMap — env var vs volume mount

![[config-inject.excalidraw]]

**Chốt:** hai kỹ thuật inject ConfigMap vào container — env var (bất biến sau khi Pod khởi động) và volume mount (tự cập nhật ~30–60s sau khi ConfigMap thay đổi); chọn theo nhu cầu hot-reload.

- **Env var — chọn từng key** (`configMapKeyRef`): tên biến trong container do bạn đặt, không phụ thuộc tên key trong ConfigMap.
- **Env var — nạp toàn bộ** (`envFrom`/`configMapRef`): tất cả key thành env var, tên biến = tên key.
- **Volume mount**: mỗi key = một file trong thư mục mount; code đọc file thay vì đọc env.

**Vì sao:** env var đơn giản, mọi ngôn ngữ đọc được (`os.environ`, `process.env`, `System.getenv`). Volume mount phức tạp hơn nhưng hỗ trợ **hot-reload** — thay đổi ConfigMap mà không cần restart Pod; phù hợp config file lớn (nginx.conf, log4j.xml) mà app có cơ chế reload tự động (inotify, SIGHUP).

**Cơ chế:** env var được inject **một lần duy nhất** khi container start — kubelet đọc ConfigMap tại thời điểm tạo container, ghi vào process environment; ConfigMap thay đổi sau đó không ảnh hưởng. Volume mount dùng `configMap` volume type — kubelet sync định kỳ (~30–60s) giá trị mới từ API server vào các file trong mount point; symlink được dùng để atomic swap.

| | Env var | Volume mount |
|---|---|---|
| Cập nhật khi ConfigMap thay đổi | phải restart Pod | tự động ~30–60s |
| Code đọc | `process.env.KEY` | `fs.readFile('/etc/config/KEY')` |
| Phù hợp | config đơn giản, ít thay đổi | config file lớn, cần hot-reload |
| Giá trị không hợp lệ tên env | không dùng được (dấu chấm…) | ok (tên file linh hoạt hơn) |

**Dùng / KHÔNG:** env var cho hầu hết use case (đơn giản, dễ debug). **Phản đề:** nếu ConfigMap có key tên `enemies.cheat.level` (có dấu chấm) → không dùng được làm tên env var; phải dùng volume mount hoặc đặt tên khác.

**Làm:**
```bash
# ConfigMap đã có từ mục 2
# Pod đọc qua env var
cat > /tmp/cm-env.pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: cm-env
spec:
  containers:
  - name: app
    image: busybox
    command: ["sh", "-c", "echo ENEMIES=$ENEMIES && sleep 3600"]
    env:
    - name: ENEMIES
      valueFrom:
        configMapKeyRef:
          name: app-settings
          key: ENEMIES
EOF
kubectl apply -f /tmp/cm-env.pod.yml
kubectl logs cm-env

# Pod đọc qua volume mount
cat > /tmp/cm-vol.pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: cm-vol
spec:
  volumes:
  - name: cfg
    configMap:
      name: app-settings
  containers:
  - name: app
    image: busybox
    command: ["sh", "-c", "sleep 3600"]
    volumeMounts:
    - name: cfg
      mountPath: /etc/config
EOF
kubectl apply -f /tmp/cm-vol.pod.yml
kubectl exec cm-vol -- ls /etc/config
kubectl exec cm-vol -- cat /etc/config/ENEMIES
```
**Kết quả:**
```text
$ kubectl logs cm-env
ENEMIES=aliens                   ← env var được inject đúng

$ kubectl exec cm-vol -- ls /etc/config
ENEMIES   LIVES                  ← mỗi key = một file

$ kubectl exec cm-vol -- cat /etc/config/ENEMIES
aliens                           ← nội dung file = value của key
```
→ **Verify:** `cm-env` in env var qua log; `cm-vol` có file `ENEMIES` trong `/etc/config` chứa `aliens`.

> **Thực chạy — cơ chế atomic swap của volume ConfigMap.** `ls -la /etc/config/` cho thấy mount KHÔNG phải file thường mà là **symlink 3 lớp**: `ENEMIES -> ..data/ENEMIES`, `..data -> ..2026_08_13_08_11_19.xxx/` (thư mục timestamp), và file thật nằm trong thư mục timestamp. Khi ConfigMap đổi, kubelet tạo thư mục timestamp **MỚI** với toàn bộ giá trị mới, rồi **atomic swap** symlink `..data` sang thư mục mới (một thao tác `rename`) — app đọc file không bao giờ thấy trạng thái half-written. Lab thật: sau `kubectl patch cm ... ENEMIES=zombies`, `..data` đổi từ `..08_11_19` sang `..08_13_58` (~30–60s sau), `cat ENEMIES` trả `zombies` mà Pod KHÔNG restart.

> **Thực chạy — env "đóng băng" chỉ đúng trong vòng đời MỘT container.** `command: ["sh","-c","... && sleep 3600"]` → sau đúng 1h `sleep` hết, container exit 0, `restartPolicy: Always` restart → env var được **đọc LẠI** ConfigMap. Nên nếu ConfigMap đã đổi thành `zombies` giữa chừng, `cm-env` sau restart sẽ thấy `zombies`, không còn `aliens`. "Env bất biến" = bất biến *trong một lần chạy container*, không phải mãi mãi. Ngoài ra `kubectl exec ... set` cho thấy loạt biến `KUBERNETES_SERVICE_HOST`, `KUBERNETES_PORT_443_TCP_*` — do `enableServiceLinks` tự tiêm địa chỉ Service trong namespace, không phải từ ConfigMap.

---

## 4. Secret — khái niệm, tạo và dùng

**Chốt:** Secret là ConfigMap dành cho dữ liệu nhạy cảm — cú pháp gần như giống hệt, nhưng K8s có thêm cơ chế giảm attack surface. Điều quan trọng nhất cần nhớ: **base64 không phải mã hoá**, Secret manifest trong git thường là rủi ro.

- **Secret** lưu password, token, certificate, SSH key — không lưu vào ConfigMap (lộ rõ trong `kubectl get cm -o yaml`).
- K8s chỉ gửi Secret tới **node nào có Pod đang cần** → giảm attack surface.
- Secret lưu trong **tmpfs trên node** → không ghi ra disk.
- Dữ liệu trong `etcd` cần enable **encryption at rest** riêng (mặc định không mã hoá).
- **Base64 encoding ≠ mã hoá** — bất kỳ ai `base64 --decode` đều đọc được ngay.

**Vì sao:** tách Secret khỏi ConfigMap để áp dụng RBAC riêng (chỉ admin được `get secret -o yaml`), audit log riêng, và policy riêng. Không commit Secret manifest dạng thô vào git → dùng SealedSecrets, Vault, hoặc external-secrets cho GitOps.

**Cơ chế:** Secret được encode base64 khi lưu vào etcd (chỉ để tránh vấn đề binary data, không phải bảo mật). Khi inject vào container qua env var hoặc volume, **K8s tự decode** trước — container nhận plaintext. Volume mount Secret: file chứa plaintext (không phải base64). Env var Secret: biến chứa plaintext.

> **Ẩn dụ:** Secret = tủ khóa trong cluster — có khóa riêng, không ai đi ngang nhìn thấy, nhưng nếu bạn chụp ảnh tủ rồi đăng lên git (manifest thô) thì khóa không còn ý nghĩa gì.

| | ConfigMap | Secret |
|---|---|---|
| Dùng cho | config thường | credential, token, cert |
| Base64 | | (encoding, không phải mã hoá) |
| tmpfs trên node | | |
| Chỉ gửi tới node cần | | |
| Commit manifest vào git | OK | Cần SealedSecrets/Vault |

**Phản đề:** Secret base64 **không an toàn** nếu ai có quyền `kubectl get secret -o yaml` hoặc đọc etcd — đây là lý do SealedSecrets tồn tại (encrypt bằng public key, chỉ controller trong cluster có private key mới decrypt được).

**Làm:**
```bash
# thấy rõ base64 không phải mã hoá
echo -n "mypassword" | base64
echo -n "bXlwYXNzd29yZA==" | base64 --decode

# tạo Secret
kubectl create secret generic db-passwords \
  --from-literal=db-password=s3cr3t \
  --from-literal=db-root-password=r00t

kubectl get secret db-passwords -o yaml
kubectl get secret db-passwords -o jsonpath='{.data.db-password}' | base64 --decode

# Pod dùng Secret qua env var + volume mount
cat > /tmp/secret-pod.yml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: secret-test
spec:
  volumes:
  - name: secrets-vol
    secret:
      secretName: db-passwords
  containers:
  - name: app
    image: busybox
    command: ["sh", "-c", "echo DB_PASS=$DATABASE_PASSWORD && sleep 3600"]
    env:
    - name: DATABASE_PASSWORD
      valueFrom:
        secretKeyRef:
          name: db-passwords
          key: db-password
    volumeMounts:
    - name: secrets-vol
      mountPath: /etc/db-passwords
EOF
kubectl apply -f /tmp/secret-pod.yml
kubectl logs secret-test
kubectl exec secret-test -- cat /etc/db-passwords/db-password
```
**Kết quả:**
```text
$ echo -n "mypassword" | base64
bXlwYXNzd29yZA==

$ echo -n "bXlwYXNzd29yZA==" | base64 --decode
mypassword                       ← decode ngay không cần key → không phải mã hoá

$ kubectl get secret db-passwords -o yaml
data:
  db-password: czNjcjN0         ← base64, không phải mã hoá
  db-root-password: cjAwdA==

$ kubectl get secret db-passwords -o jsonpath='{.data.db-password}' | base64 --decode
s3cr3t                           ← plaintext

$ kubectl get cm,secret
NAME                         DATA   AGE
configmap/app-settings       2      5m
configmap/kube-root-ca.crt   1      5d

NAME                       TYPE     DATA   AGE
secret/db-passwords        Opaque   2      2m

$ kubectl logs secret-test
DB_PASS=s3cr3t               ← K8s tự decode base64 trước khi inject env

$ kubectl exec secret-test -- cat /etc/db-passwords/db-password
s3cr3t                       ← file trong volume cũng là plaintext
```
→ **Verify:** env var và volume file đều chứa plaintext — K8s tự decode. `kubectl get cm,secret` thấy cả hai resource cùng lúc.

---

## Dọn dẹp
```bash
kubectl delete pod cm-env cm-vol secret-test --ignore-not-found
kubectl delete configmap app-settings --ignore-not-found
kubectl delete secret db-passwords --ignore-not-found
```

---

## Đủ khi (nói trơn bằng lời mình)
① Vì sao tách config khỏi image — 12-factor + multi-env + multi-node · ② 4 cách tạo ConfigMap và kết quả `data` khác nhau thế nào (`--from-file` vs `--from-env-file`) · ③ env var vs volume mount — cập nhật động hay phải restart · ④ base64 ≠ mã hoá — hệ quả với Secret manifest trong git thường · ⑤ `secretKeyRef` vs `configMapKeyRef`; vì sao cần SealedSecrets trong GitOps.

## Recall — tự kiểm (cuối buổi)
Tự trả lời trước, xong hết mới cuộn xuống Đáp án.

**ConfigMap:**
1. Vì sao không bake cứng config vào Docker image?
2. `--from-file` và `--from-env-file` khác nhau thế nào ở kết quả data trong K8s?
3. Cú pháp YAML để đọc một key cụ thể từ ConfigMap vào env var là gì?
4. Cú pháp để nạp toàn bộ ConfigMap vào env var?
5. Volume mount từ ConfigMap có tự cập nhật không? Env var thì sao?

**Secret:**
6. Base64 có phải mã hoá không? Hệ quả là gì khi commit Secret manifest vào git thường?
7. Secret lưu ở đâu trên node? Lưu ở đâu trên master?
8. K8s có gửi Secret tới mọi node không? Tại sao?
9. Khác gì giữa `secretKeyRef` và `configMapKeyRef`?
10. Vì sao cần SealedSecrets trong GitOps? Base64 chưa đủ sao?

### Đáp án

1. Config thay đổi theo env (dev/staging/prod) → phải build lại image mỗi lần; vi phạm "một image, nhiều môi trường". Trong K8s, Pod schedule bất kỳ node nào → không drop file lên host được.
2. `--from-file`: tên file = key, toàn bộ nội dung file = value (một blob). `--from-env-file`: mỗi dòng `KEY=value` = một cặp riêng biệt trong `data` → giống manifest.
3. `env: - name: VAR_NAME` → `valueFrom: configMapKeyRef: name: <cm-name> key: <key>`.
4. `envFrom: - configMapRef: name: <cm-name>` — tất cả key thành env var, key = tên biến.
5. Volume mount **tự cập nhật** (~30–60s) khi ConfigMap thay đổi. Env var **không cập nhật** — phải restart Pod.
6. Không — base64 decode được ngay bằng lệnh command line. Commit vào git thường = bất kỳ ai clone repo đều đọc được secret.
7. Trên node: **tmpfs** (không ghi ra disk). Trên master: **etcd** (cần encryption at rest riêng).
8. Không — K8s chỉ gửi Secret tới node có Pod đang cần nó → giảm attack surface.
9. Chỉ khác tên trường: `secretKeyRef` thay vì `configMapKeyRef`. K8s tự decode base64 trước khi inject vào container — container nhận plaintext.
10. Base64 không phải mã hoá — ai có quyền đọc etcd hoặc `kubectl get secret -o yaml` đều đọc được. SealedSecrets encrypt bằng public key của cluster; chỉ controller trong cluster có private key mới decrypt được → an toàn khi commit vào git.
