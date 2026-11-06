#!/usr/bin/env python3
"""Record facilitator-observed usability tasks; no synthetic participant data.

The facilitator prepares the six scenarios in docs/evaluation/usability.md and
compares each participant answer to the known answer without coaching. Results
are written only after an actual interactive session, using exclusive creation.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from time import monotonic

TASKS = (
    ('BR-01', 'Tìm đợt khí hậu và giải thích coverage.'),
    ('BR-02', 'Xem ngày tưới/drain và tỷ lệ paired.'),
    ('BR-03', 'Tìm đợt EC/pH và giải thích chất lượng.'),
    ('BR-04', 'Kiểm tra năng lượng trên kg loại A và điều kiện tính.'),
    ('BR-05', 'Tìm kỳ hoặc record có tỷ lệ loại B cao.'),
    ('BR-06', 'Tạo và giải thích scenario thay đổi giá điện.'),
)


def required(prompt):
    while True:
        result = input(prompt).strip()
        if result:
            return result
        print('Cần nhập một giá trị; dùng "không có" nếu phù hợp.')


def choice(prompt, allowed):
    while True:
        result = required(prompt).lower()
        if result in allowed:
            return result
        print('Chọn một trong: ' + ', '.join(allowed))


def record_session(participant, context, run_input=input, clock=monotonic, now=None):
    # Input/clock parameters let test code exercise collection without claiming
    # the synthetic transcript is a human observation.
    def ask(prompt):
        while True:
            value = run_input(prompt).strip()
            if value:
                return value
            print('Cần nhập một giá trị; dùng "không có" nếu phù hợp.')

    def ask_choice(prompt, options):
        while True:
            value = ask(prompt).lower()
            if value in options:
                return value
            print('Chọn một trong: ' + ', '.join(options))

    started = (now or (lambda: datetime.now(timezone.utc)))()
    results = []
    for br, description in TASKS:
        print(f'\n{br}: {description}')
        run_input('Nhấn Enter khi người tham gia bắt đầu tác vụ...')
        start = clock()
        run_input('Nhấn Enter khi tác vụ kết thúc hoặc người tham gia dừng...')
        duration = clock() - start
        if duration < 0:
            raise ValueError('Đồng hồ đo thời gian không hợp lệ')
        results.append({
            'br': br,
            'elapsed_seconds': round(duration, 3),
            'completion': ask_choice('Kết quả [khong_tro_giup/co_tro_giup/khong_xong]: ',
                                     ('khong_tro_giup', 'co_tro_giup', 'khong_xong')),
            'answer': ask('Câu trả lời quan sát được: '),
            'answer_correct': ask_choice('Đối chiếu đáp án [dung/sai/chua_danh_gia]: ',
                                         ('dung', 'sai', 'chua_danh_gia')),
            'coverage_explanation': ask('Giải thích coverage/NULL/giới hạn: '),
            'explanation_correct': ask_choice('Giải thích [dung/sai/chua_danh_gia]: ',
                                              ('dung', 'sai', 'chua_danh_gia')),
            'feedback': ask('Phản hồi/vướng mắc: '),
        })
    return {'version': 1, 'participant_pseudonym': participant,
            'started_at_utc': started.isoformat(), 'context': context, 'tasks': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Ghi quan sát người tham gia cho sáu BR.')
    parser.add_argument('--participant', required=True, help='Mã ẩn danh, ví dụ P01')
    parser.add_argument('--output', type=Path, required=True, help='File JSON mới; không ghi đè')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', args.participant):
        parser.error('Mã ẩn danh chỉ gồm chữ, số, dấu _ hoặc -, tối đa 32 ký tự')
    # Refuse early without clobbering and check again atomically on creation.
    if args.output.exists():
        parser.error('File kết quả đã tồn tại; chọn đường dẫn mới')
    print('Chuẩn bị publication, bộ lọc và đáp án trước. Không ghi tên hay thông tin cá nhân.')
    context = {
        'participant_type': choice('Người tham gia [nguoi_dung/thanh_vien_gia_lap]: ',
                                   ('nguoi_dung', 'thanh_vien_gia_lap')),
        'experience': required('Vai trò và mức quen ứng dụng (ẩn danh): '),
        'application_version': required('Commit/image version: '),
        'dataset_publication_rules': required('Dataset, publication, cửa sổ, rule/basis/scenario: '),
    }
    session = record_session(args.participant, context)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(session, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(f'Đã lưu quan sát tại {args.output}; kiểm tra lại trước khi đưa vào báo cáo.')


if __name__ == '__main__':
    main()
