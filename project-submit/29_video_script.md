# Group 29 — Kịch bản project 24 phút

Video thuyết trình tổng thể **20–30 phút do user quay offline**. Bảy walkthrough
ClickHouse đã có là bằng chứng thao tác, không thay cho video thuyết trình.
Tổng 27 phút 22,56 s của cả bộ không được phát hết bên trong kịch bản 24 phút.
Mỗi BR chỉ chèn highlight khoảng 30–45 s hoặc demo live ngắn, rồi giải thích.

Dùng ba vai A/B/C; tên/MSSV là placeholder trong `docs/submission/group.json`.
Điền file đó rồi chạy `./scripts/build_submission.sh` để cập nhật cả hai PDFs. Phân công này là gợi ý
trình bày cần nhóm xác nhận, không tự ghi nhận đóng góp thực tế. A giải thích
BR-01/03; B giải thích BR-02/05; C giải thích BR-04/06. Có handoff ngắn giữa các
người, tránh một người đọc hết phần của người khác.

| Thời gian | Vai | Slide và nội dung |
|---|---|---|
| 00:00–02:00 | A | Slide 1–2: người dùng, quyết định được hỗ trợ, sáuBR và phân công. |
| 02:00–04:00 | B | Slide 3–4: AGC, ba grain, NULL/quality/timezone; basis 62,5 m² giả định. |
| 04:00–07:00 | C | Slide 5–8: CH native batch Python, Spark stream thật; publication/ACK/checkpoint. |
| 07:00–09:00 | A+B | Slide 9: A chọn rule/episode khí hậu; B giải thích tưới/drain, paired ratio vàCSV. |
| 09:00–11:30 | A+B | Slide 10: A chỉ EC/pH/gap; B chỉ share B Digilog 19/02 và Reference incomplete. |
| 11:30–14:00 | C | Slide 11:308,078 kWh, A=0→NULL; baseline 17,6001 EUR, điện +20%→20,8407,total+18,41%; frozen context/save/reopen. |
| 14:00–16:00 | A | Slide 12: replay 10→30,MQTT/Spark, source clock/freshness, complete vs collector stale. |
| 16:00–18:00 | B | Slide 13–14: oracle 6 BR/96 CSV/30 lineage/67.829 episode; restore/rollback; hai phép so riêng. |
| 18:00–20:00 | C | Slide 15–16:warm p95,cold cost, PG nhanh hơn workload đã đo; không gộp benchmark. |
| 20:00–22:00 | A+B | Slide 17: walkthrough oracle, lỗi đã sửa; chưa có user study, không suy hiệu quả thực tế. |
| 22:00–24:00 | C | Slide 18–19:local setup, bàn giao, giới hạn; kết luận và mời hỏi. |

## Chọn đúng dữ liệu trước khi nói số

- Video gốc ghim `4b08bd51c092465783304f887d766d47`, Automatoes 02–03/03/2020:
  Irr 9,36/drain 3,89 L/m²→41,56%; raw nguyên bản.
- App hiện hành ghim `5d455686a06649e789908b9708a9d436`: correction diễn tập
  `cutover-resource-20261107` cộng 1 Irr ngày 02/03, tổng 10,36/drain 3,89→37,55%.
  Giải thích audit+pin mới; không nói 10,36 là quan sát AGC gốc. Energy/harvest
  vẫn cùng giá trị vì correction chỉ đổi Irr. PG rollback c51402d9…khớp pin mới.
- Catalog UI chọn current complete pin; không hứa chọn pin cũ trong sidebar.
  Muốn thuyết trình raw 9,36 dùng clip đã ghim 4b08 và ghi rõ. Muốn demo live dùng
  số 10,36 và context hiện hành; không sửa warehouse để làm hình khớp clip.
- Scenario đã lưu có frozen publication/context. Không ép mở scenario 4b08 lên 5d45.
  Dùng clip save/reopen hoặc chọn scenario đúng context; tạo mới chỉ khi cần và
  ghi lại request/version. Dryrun ưu tiên thao tác đọc để không đổi bản nghiệm thu.

## Checklist trước và sau ghi hình

1. Điền 3 tên/MSSV ở report/slides, xác nhận vai/đóng góp; build lại PDFs.
2. Chuẩn bị image/ZIP trước; mở localhost và làm warm proof trước khi quay.
   Cold proof có thể vài phút, không cắt ghép rồi tuyên bố startup≤2 s.
3. Mở script, PDFs và bảy video local; đối chiếu publication/metric/source/version.
   Hiển thị đủ dropdown, quality, CSV; dùng highlights theo nhịp trên.
4. Test mic/âm lượng và chuyển vai. Không lộ `.env`, password hoặc log cá nhân.
5. Ghi 24 phút dự kiến, xuất `project-submit/29_video.mp4` hoặc `.webm` có audio.
6. Xem/nghe/giải mã toàn bộ, kiểm 20–30 phút, chữ đọc được, đủ ba vai và sáuBR.
7. Chạy kiểm gói cuối (không dùng `--pre-video`), ghi checksum video và kiểm
   quyền truy cập nơi nộp. Không tự upload/nộp khi chưa chốt nơi nhận.

Metadata/checksums walkthrough: `docs/evaluation/assets/clickhouse-walkthrough/`;
video local: `project-submit/demo-videos/clickhouse/index.html`. Replay là nguồn
lịch sử phát lại; basis/rules có nhãn; chưa có user study/hardware. Giữ các giới
hạn này trong lời nói, không thay bằng kết luận tăng năng suất/tiết kiệm thật.
