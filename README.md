# mayzing-bulk-upload

**Luồng hiện tại:** mở Chrome thường → đăng nhập thủ công → kết nối tab bằng
**Mayzing Session Bridge** → bấm **Bật tự động upload**. Cookie vẫn nằm trong Chrome;
tool không dùng Playwright. Xem [hướng dẫn tiện ích](chrome-extension/README.md).
Tự động chỉ hoạt động sau khi bạn chủ động kết nối và bật; mất kết nối sẽ dừng job.

Công cụ Windows tạo hàng loạt campaign trên **Mayzing** (dashboard.mayzing.com) từ file design,
làm theo cách của tool PodSach:

1. Nạp file design (PNG trong suốt tốt nhất), đặt lên canvas in **4200 x 4800 px @ 300 dpi**
   (khổ Mayzing khuyên dùng). File đã đúng khổ được giữ nguyên từng pixel.
2. Đọc câu quote trên design làm **tiêu đề campaign** (Claude; không có key thì gõ tay).
3. Lấy **URL** từ tên file (`ge123 front.png` -> `/ge123`, `216.png` -> `/216`); tên file không có mã
   thì URL theo tiêu đề (`ICH KAM ICH SAH` -> `/ich-kam-ich-sah`). URL đã có trên store / đã dùng ở
   batch trước thì thêm chữ cái (`ge123a`) hoặc `-2`.
4. Trang **Duyệt**: sửa quote / URL / origin, Duyệt hoặc Loại từng thẻ, **Xuất batch**.
5. **Upload**: tải artwork lên Artwork library, chạy **Bulk clone** từ campaign gốc (origin), đặt tên
   + URL cho từng campaign mới, thêm tag, rồi **Publish changes**.

Bulk clone của Mayzing sao chép **sản phẩm, giá, màu và mockup** của campaign gốc cho mọi artwork
mới. Vì vậy chỉ cần chuẩn bị sẵn 1-3 campaign gốc trên store (ví dụ một gốc cho design chữ đen in áo
sáng, một gốc cho design chữ trắng in áo tối).

---

## 1. Cài đặt (một lần)

Yêu cầu: Windows 10/11, Python 3.11+ (https://www.python.org/downloads/, tick "Add Python to PATH"),
Google Chrome.

Tool hỗ trợ dùng trực tiếp Chrome cài trên máy: đặt
`BROWSER_EXECUTABLE_PATH="C:/Program Files/Google/Chrome/Application/chrome.exe"` trong `.env`.
Đường dẫn sai sẽ báo lỗi để bạn sửa lại.
Chrome mở bình thường bằng hồ sơ bạn chọn; không dùng hồ sơ tự động hóa riêng.

1. Double-click `setup.bat` (tạo `.venv`, cài thư viện, tạo `.env`).
2. Mở `.env`, dán key Anthropic vào `ANTHROPIC_API_KEY=` nếu muốn tool tự đọc quote.
3. Double-click `run_dashboard.bat` -> trình duyệt mở `http://127.0.0.1:8780/`.
4. Tab **Mayzing** -> **Mở Chrome thường**, mở shop và đăng nhập thủ công trong hồ sơ Chrome của bạn.
   Nạp [Mayzing Session Bridge](chrome-extension/README.md) vào đúng hồ sơ đó. Trong tool bấm
   **Tạo mã kết nối**, mở tiện ích ở tab shop, dán mã và kết nối. Mã có hạn **24 giờ**, dùng một lần.
   Quay lại tool bấm **Bật tự động upload**; tool kiểm tra phiên và đọc danh sách store.
   Giữ tab shop mở. Cookie và mật khẩu ở trong Chrome; tool không đọc hoặc xuất cookie.
   Khởi động lại Chrome hoặc tool thì tạo mã mới và ghép lại. Khi Mayzing yêu cầu xác minh,
   tool dừng để bạn hoàn tất trực tiếp trên trang.
5. Vẫn ở tab Mayzing: chọn **Store**, chọn **campaign gốc** cho design tối / design sáng / mặc định,
   tag thêm (nếu có), bấm **Lưu**.

## 2. Dùng hằng ngày (dashboard)

Giao diện giống dashboard PodSach: cột trái là các bước quy trình, job đang chạy (thanh tiến độ, nút
"Xem log") và thông tin store / OCR / origin / session; góc trên phải là store đang kết nối và trạng thái job.

| Tab | Việc làm |
|---|---|
| **1. Design** | Kéo thả file design (PNG / JPG / WebP) -> lưu vào `input\` (lưới ảnh, xoá từng file / xoá toàn bộ). Chọn "Đọc chữ bằng" (Claude / không đọc) và "Origin cho cả batch" (trống = theo tông màu), bấm **Chạy Prep**. |
| **2. Duyệt** | Lọc Tất cả / Chưa duyệt / Cảnh báo / Đã duyệt / Đã loại, ô tìm kiếm. Mỗi file một thẻ: URL ngay trên đầu thẻ, ảnh in bên trái (nền ca-rô / tối / trắng / xám), ô đỏ "Cần xử lý tay", cờ cảnh báo, quote -> tiêu đề ngắn (mặc định 8 từ / 60 ký tự), Origin riêng. **Duyệt** / **Loại** (phím `A` / `R`, `J`/`K` chuyển thẻ, `E` sửa quote), "Duyệt tất cả còn lại", "Huỷ kết quả", **Xuất batch** -> `output\<batch>\` + `manifest.csv`, file design chuyển sang `input\done\<batch>\`. |
| **3. Batch & upload** | Tuỳ chọn upload (tag thêm, tự Publish), nút **Publish changes**. Mỗi batch một bảng: **Kiểm tra** (chỉ đọc: session, origin, URL trùng), **Upload N lên Mayzing** (tạo campaign thật, có hộp xác nhận), **Thử lại lỗi**, **Log**, **Xoá**; từng dòng chưa xong có **Sửa** (tiêu đề / URL) và "bỏ qua". |
| **Mayzing** | Store đang kết nối, mở Chrome thường, mã ghép 24 giờ, bật/tắt tự động, origin theo tông màu và sản phẩm hiện đầu tiên cho campaign mới. |

Cửa sổ lệnh của `run_dashboard.bat` phải để mở trong lúc dùng (sửa code thì tắt cửa sổ đó và chạy
lại). Dashboard chạy một việc một lúc (prep / upload / đăng nhập); log hiện ở ngăn dưới cùng.

**Chọn campaign gốc**: thẻ dùng origin theo tông màu design (design tối -> "Origin cho design tối",
design sáng -> "Origin cho design sáng", design nhiều màu -> "Origin mặc định"). Ô "Campaign gốc"
trên thẻ đổi được origin riêng cho từng design (ví dụ campaign in mặt sau).

**Cờ trên thẻ**: đỏ = phải xử lý (`chưa có tiêu đề`, `chưa có URL`, `file không trong suốt`, `file quá
nhỏ`, `quote đọc không chắc`...), vàng = nên xem (`phóng to`, `URL đã có, thêm hậu tố`, `chưa kiểm tra
URL trên Mayzing`...). Thẻ có lỗi chặn (thiếu tiêu đề / URL / design) không duyệt được.

## 3. Upload chạy như thế nào

Với mỗi campaign gốc của batch, tối đa 50 artwork một lần:

1. Tải từng file in lên **Artwork library**, tên ảnh = tiêu đề campaign (Bulk clone đặt tên campaign
   mới theo tên artwork).
2. Gọi **Bulk clone** của campaign gốc với các artwork đó; chờ task xong (giống Task log trên Mayzing).
3. Tìm campaign mới, đổi **URL** thành slug của dòng, đặt tên = tiêu đề, tag = tag của gốc + tag thêm.
4. Cuối lượt: **Publish changes** (nếu bật).

Mỗi dòng ghi `done` / `error: ...` vào `manifest.csv` ngay khi xong, kèm link campaign. Chạy lại an
toàn: dòng `done` được bỏ qua; dòng có URL đã thuộc một campaign cùng tiêu đề (lần chạy trước bị ngắt)
được đánh dấu xong, không tạo trùng. Tool ghi dấu trước khi gửi clone; nếu kết quả chưa rõ,
chỉ tiếp tục campaign đã xác định được hoặc yêu cầu kiểm tra Task log. URL đã thuộc campaign khác
-> lỗi, sửa URL trong bảng rồi "Thử lại lỗi".

Upload và publish chạy từ **dashboard** để dùng kết nối Chrome trong cùng tiến trình.
`run_login.bat` chỉ mở Chrome thường; các lệnh upload CLI cũ không chia sẻ phiên đã ghép với dashboard.

## 4. Đọc quote (Claude)

`CLAUDE_MODEL` (mặc định `claude-haiku-5-5`) đọc mọi design. Không chắc (độ tin cậy không phải "high",
không đọc ra chữ, hoặc lỗi) thì `CLAUDE_HARD_MODEL` (mặc định `claude-opus-5-5`, effort high) đọc lại
có kèm bản đọc đầu. Vẫn không chắc -> cờ đỏ "quote đọc không chắc", đọc tay trên thẻ. Quote giữ nguyên
ngôn ngữ in trên áo (Đức, Anh, ...), cả umlaut. Tiêu đề: viết hoa (`TITLE_CASE=as_printed` để giữ
nguyên), lấy phần đầu quote, mặc định tối đa 8 từ và 60 ký tự, cắt tại ranh giới từ.
Quote đầy đủ vẫn được giữ trên thẻ. Có thể chỉnh `TITLE_MAX_WORDS` / `TITLE_MAX_LENGTH` trong `.env`
(Mayzing cho phép tối đa 255 ký tự). Đổi model trong `.env`.

## 5. Cấu hình

**Sản phẩm hiện đầu tiên:** trong tab Mayzing, chọn theo từng origin ở mục
"Sản phẩm hiện đầu tiên cho campaign mới". Tool tìm đúng loại sản phẩm trong campaign vừa clone,
đưa sản phẩm đó lên đầu và đặt làm Cover product trước khi publish. Với campaign đã tạo,
vào Batch & upload → **Sản phẩm đầu** ở từng dòng để chọn và lưu trực tiếp trên Mayzing.
Khách mở link campaign chung sẽ thấy sản phẩm mặc định sau khi Publish changes hoàn tất.
Tính năng này cần tiện ích Mayzing Session Bridge **1.1.0** trở lên.

`config.py` (có chú thích), `.env` (key, model), `settings.json` (dashboard tự ghi: store, origin, tag).
Hay chỉnh: `ARTWORK_CANVAS_SIZE`, `ARTWORK_WIDTH_FRACTION`, `ARTWORK_TOP_FRACTION`, `ARTWORK_MODE`
(`original` = tải file nguyên bản), `FORBIDDEN_WORDS`, `CLONE_MAX_PER_TASK`, `DELETE_UPLOADED_FILES`.

`used_slugs.txt` lưu mọi URL đã xuất để không bao giờ dùng lại. Đừng xoá file này.

## 6. Thư mục

```
dashboard.py, lib/dashboard.html   giao diện web local
upload_batch.py                    tạo campaign trên Mayzing (Bulk clone)
lib/mayzing.py                     đăng nhập + các lệnh GraphQL của dashboard Mayzing
lib/review.py                      prep, thẻ duyệt, xuất batch
lib/artwork.py                     canvas in, tông màu, cảnh báo kích thước
lib/ocr.py                         đọc quote bằng Claude
lib/slug.py, lib/titles.py         quy tắc URL / tiêu đề
lib/manifest.py                    manifest.csv
tools/selftest.py                  tự kiểm tra offline (không cần mạng)
input\  output\<batch>\  logs\<batch>.log  auth\chrome-profile\ (hồ sơ Chrome của tool, giữ đăng nhập - không chia sẻ)
```

## 7. Sự cố

- **Nút "Đăng nhập (mở Chrome)" bị khoá**: đang có job chạy. Nếu là đăng nhập, bấm **Dừng đăng nhập**
  rồi mở lại. Lỗi khởi động dịch vụ Chrome được trả về ngay; mỗi lần khởi chạy Chrome giới hạn 30 giây.
- Mỗi cổng chỉ chạy một dashboard; mở lại `run_dashboard.bat` sẽ dùng dashboard đang chạy.
- **Cloudflare "Chờ một chút" lặp lại sau khi đăng nhập**: tool dừng và hiện "cần xác minh Cloudflare",
  không tự tải lại trang hay gửi upload. Hoàn tất xác minh trực tiếp trên trang; khi thấy dashboard
  Mayzing, bấm **Kiểm tra session**. Nếu vẫn lặp, liên hệ hỗ trợ Mayzing về cách tích hợp được hỗ trợ.
  Dùng Chrome thật không bảo đảm Cloudflare chấp nhận một phiên do Playwright điều khiển.
- Chrome của tool bật sandbox; không dùng `--no-sandbox`. Không xoá hồ sơ hay cookie khi gặp xác minh.
- **"Chưa đăng nhập Mayzing"**: tab Mayzing -> Đăng nhập, đăng nhập trong cửa sổ Chrome của tool.
- **"Hồ sơ Chrome của tool đang được một tiến trình khác dùng"**: dashboard đang chạy - upload từ dashboard
  (hoặc tắt dashboard rồi chạy `run_upload.bat`).
- **Đăng nhập bằng Google bị báo "trình duyệt không an toàn"**: Google chặn trình duyệt do phần mềm điều
  khiển; dùng đăng nhập email + mật khẩu của Mayzing trong cửa sổ đó.
- **"không thấy campaign gốc"**: origin bị xoá / đổi store -> chọn lại ở tab Mayzing.
- **"URL ... đã thuộc campaign khác"**: sửa URL của dòng trong bảng batch, bấm "Thử lại lỗi".
- **"Bulk clone xong nhưng không tìm thấy campaign mới"**: xem Task log trên Mayzing; campaign có thể
  đã được tạo với tên artwork - sửa tay trên Mayzing rồi bấm "bỏ qua" dòng đó.
- **Không mở được Chrome**: cài Google Chrome, hoặc đặt `BROWSER_CHANNEL=` trong `.env` và chạy
  `.venv\Scripts\python.exe -m playwright install chromium`.
