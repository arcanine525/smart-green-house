# Bằng chứng đánh giá

Scaffold và synthetic stack tests chỉ chứng minh môi trường chạy. Sáu BR vẫn chưa nghiệm thu toàn bộ; xem [requirements matrix](../implementation/requirements-matrix.md).

| BR | Bằng chứng chức năng | Trạng thái |
|---|---|---|
| BR-02 (nước) | [br_02.md](br_02.md): đủ AC-01…AC-10, đối soát raw↔publication AGC, timings | Chức năng đạt trên fixture + cửa sổ AGC; basis nước vẫn `assumed_unverified` |

T13/T15/T17/T19/T21 sẽ ghi các BR còn lại; T23 correctness, T25 performance, T26 usability và T36 sensor replay. Không tạo báo cáo pass khi chưa chạy các task đó. Bằng chứng scaffold T04 được ghi trong [hướng dẫn môi trường](../implementation/local-environment.md).
