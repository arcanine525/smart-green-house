# Kiểm kê snapshot AGC 2nd edition

Số liệu được đo từ snapshot đã khóa; đây là kiểm kê nguồn, không phải dữ liệu typed/quality hoặc bằng chứng nghiệm thu BR.

- Snapshot ID: `agc-2nd-2019-c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`.
- Archive: SHA-256 `c19d385e7f4731a77e59ebfd763fd49e35e7ac4128d0aaf41b5717709cd75a68`, 12,793,984 byte.
- 45 file = 43 CSV + 2 PDF; 103,682,625 byte giải nén; tổng 622,899 dòng dữ liệu CSV (không tính header).
- Nguồn: [Kaggle AGC](https://www.kaggle.com/datasets/piantic/autonomous-greenhouse-challengeagc-2nd-2019/). ReadMe.pdf và Economics.pdf được giữ nguyên trong snapshot, có hash/size trong manifest.
- Thư mục nguồn chỉ là định danh thô; mapping khoang/team, timezone, basis diện tích và ngữ nghĩa setpoint/giá thuộc T03. Không tự sửa ngày hoặc gắn UTC.

## File, số dòng/cột và thời gian nguồn

Thời gian hiển thị dùng epoch Excel 1899-12-30, không timezone, chỉ để đọc inventory. Cadence là median delta dương giữa dòng liên tiếp; không khẳng định mỗi khoảng đều đủ/đều hoặc timestamp đại diện đầu/cuối slot.

Median thô 4.9968 phút trong các chuỗi dày tương đương xấp xỉ 5 phút; không làm tròn hoặc sửa serial trong snapshot. Chính sách slot/timezone thuộc T03.

| File | Dòng | Cột header | Min serial | Max serial | Min hiển thị | Max hiển thị | Median delta (phút) |
|---|---:|---:|---|---|---|---|---:|
| `AICU/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `AICU/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `AICU/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `AICU/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `AICU/Production.csv` | 24 | 9 | 43875 | 43980 | 2020-02-14 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `AICU/Resources.csv` | 166 | 7 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `AICU/TomQuality.csv` | 8 | 8 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `Automatoes/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `Automatoes/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Automatoes/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Automatoes/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `Automatoes/Production.csv` | 23 | 9 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `Automatoes/Resources.csv` | 166 | 7 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `Automatoes/TomQuality.csv` | 8 | 8 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `Digilog/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `Digilog/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Digilog/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Digilog/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `Digilog/Production.csv` | 23 | 9 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `Digilog/Resources.csv` | 166 | 7 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `Digilog/TomQuality.csv` | 8 | 8 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `IUACAAS/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `IUACAAS/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `IUACAAS/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `IUACAAS/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `IUACAAS/Production.csv` | 23 | 9 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `IUACAAS/Resources.csv` | 166 | 12 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `IUACAAS/TomQuality.csv` | 8 | 8 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `Reference/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `Reference/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Reference/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `Reference/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `Reference/Production.csv` | 24 | 9 | 43510 | 43980 | 2019-02-14 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `Reference/Resources.csv` | 166 | 7 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `Reference/TomQuality.csv` | 8 | 7 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `TheAutomators/CropParameters.csv` | 23 | 6 | 43823 | 43978 | 2019-12-24 00:00:00 | 2020-05-27 00:00:00 | 10080 |
| `TheAutomators/GreenhouseClimate.csv` | 47809 | 50 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `TheAutomators/GrodanSens.csv` | 47809 | 7 | 43815.00000 | 43981.00000 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |
| `TheAutomators/LabAnalysis.csv` | 10 | 39 | 43836 | 43963 | 2020-01-06 00:00:00 | 2020-05-12 00:00:00 | 20160 |
| `TheAutomators/Production.csv` | 23 | 9 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 7200 |
| `TheAutomators/Resources.csv` | 166 | 7 | 43815 | 43980 | 2019-12-16 00:00:00 | 2020-05-29 00:00:00 | 1440 |
| `TheAutomators/TomQuality.csv` | 8 | 8 | 43880 | 43980 | 2020-02-19 00:00:00 | 2020-05-29 00:00:00 | 20160 |
| `Weather/Weather.csv` | 47809 | 11 | 43815 | 43981 | 2019-12-16 00:00:00 | 2020-05-30 00:00:00 | 4.9968 |

## PDF nguồn

| File | Byte | SHA-256 |
|---|---:|---|
| `Economics.pdf` | 1180896 | `d4b76625c9defd59bc3389099567eafce95e4ed4af0e3f4abf34e35fbeaa95bf` |
| `ReadMe.pdf` | 733072 | `e884cc347088bac498b730eaf442ccb9593c7aa84d4f85090b1fe2927646509c` |

## Header nguyên văn và khác biệt

Header được lưu nguyên whitespace/tab trong manifest; JSON dưới đây dùng escape để nhìn thấy tab. Không rename, trim, bỏ cột hoặc sửa row ở bước này. Các chỉ số cột dùng index bắt đầu từ 0.

### CropParameters.csv — 6 file

`AICU/CropParameters.csv`, `Automatoes/CropParameters.csv`, `Digilog/CropParameters.csv`, `IUACAAS/CropParameters.csv`, `Reference/CropParameters.csv`, `TheAutomators/CropParameters.csv`

```json
"%Time,Stem_elong,Stem_thick,Cum_trusses,stem_dens ,plant_dens"
```

### GreenhouseClimate.csv — 6 file

`AICU/GreenhouseClimate.csv`, `Automatoes/GreenhouseClimate.csv`, `Digilog/GreenhouseClimate.csv`, `IUACAAS/GreenhouseClimate.csv`, `Reference/GreenhouseClimate.csv`, `TheAutomators/GreenhouseClimate.csv`

```json
"%time,AssimLight,BlackScr,CO2air,Cum_irr,EC_drain_PC,EnScr,HumDef,PipeGrow,PipeLow,Rhair,Tair,Tot_PAR,Tot_PAR_Lamps,VentLee,Ventwind,assim_sp,assim_vip,co2_dos,co2_sp,co2_vip,dx_sp,dx_vip,int_blue_sp,int_blue_vip,int_farred_sp,int_farred_vip,int_red_sp,int_red_vip,int_white_sp,int_white_vip,pH_drain_PC,scr_blck_sp,scr_blck_vip,scr_enrg_sp,scr_enrg_vip,t_grow_min_sp,t_grow_min_vip,t_heat_sp,t_heat_vip,t_rail_min_sp,t_rail_min_vip,t_vent_sp,t_ventlee_vip,t_ventwind_vip,water_sup,water_sup_intervals_sp_min,water_sup_intervals_vip_min,window_pos_lee_sp,window_pos_lee_vip"
```

### GrodanSens.csv — 6 file

`AICU/GrodanSens.csv`, `Automatoes/GrodanSens.csv`, `Digilog/GrodanSens.csv`, `IUACAAS/GrodanSens.csv`, `Reference/GrodanSens.csv`, `TheAutomators/GrodanSens.csv`

```json
"%time,EC_slab1,EC_slab2,WC_slab1,WC_slab2,t_slab1,t_slab2"
```

### LabAnalysis.csv — 6 file

`AICU/LabAnalysis.csv`, `Automatoes/LabAnalysis.csv`, `Digilog/LabAnalysis.csv`, `IUACAAS/LabAnalysis.csv`, `Reference/LabAnalysis.csv`, `TheAutomators/LabAnalysis.csv`

```json
"%Time,irr_PH,irr_EC,irr_NH4,irr_K,irr_Na,irr_Ca,irr_Mg,irr_Si,irr_NO3,irr_Cl,irr_SO4,irr_HCO3,irr_PO4,irr_Fe,irr_Mn,irr_Zn,irr_B,irr_Cu,irr_Mo,drain_PH,drain_EC,drain_NH4,drain_K,drain_Na,drain_Ca,drain_Mg,drain_Si,drain_NO3,drain_Cl,drain_SO4,drain_HCO3,drain_PO4,drain_Fe,drain_Mn,drain_Zn,drain_B,drain_Cu,drain_Mo"
```

### Production.csv — 6 file

`AICU/Production.csv`, `Automatoes/Production.csv`, `Digilog/Production.csv`, `IUACAAS/Production.csv`, `Reference/Production.csv`, `TheAutomators/Production.csv`

```json
"%time,ProdA,ProdB,avg_nr_harvested_trusses,Truss development time ,Nr_fruits_ClassA,Weight_fruits_ClassA,Nr_fruits_ClassB,Weight_fruits_ClassB"
```

### Resources.csv — 5 file

`AICU/Resources.csv`, `Automatoes/Resources.csv`, `Digilog/Resources.csv`, `Reference/Resources.csv`, `TheAutomators/Resources.csv`

```json
"%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain"
```

### Resources.csv — 1 file

`IUACAAS/Resources.csv`

```json
"%Time ,Heat_cons,ElecHigh,ElecLow,CO2_cons,Irr,Drain,,,,,"
```

### TomQuality.csv — 1 file

`Reference/TomQuality.csv`

```json
"%time,\tFlavour, \tTSS,\tAcid,\t%Juice,\tBite,\tWeight\tDMC_fruit"
```

### TomQuality.csv — 5 file

`AICU/TomQuality.csv`, `Automatoes/TomQuality.csv`, `Digilog/TomQuality.csv`, `IUACAAS/TomQuality.csv`, `TheAutomators/TomQuality.csv`

```json
"%time,Flavour ,TSS,Acid,%Juice,Bite,Weight,DMC_fruit"
```

### Weather.csv — 1 file

`Weather/Weather.csv`

```json
"%time,AbsHumOut,Iglob,PARout,Pyrgeo,RadSum,Rain,Rhout,Tout,Winddir,Windsp"
```

## Quan sát cấu trúc và giá trị trống

`missing_token_counts` chỉ đếm token rỗng/NaN/NA/N/A theo chuỗi (không phân loại accepted/invalid/suspect). Row width lệch header được ghi lại, không bỏ dòng. Grain nghiệp vụ và quality policy sẽ được chốt ở T03/T06–T07.

| File | Row width → số dòng | Cột toàn rỗng (index) | Time lùi / lặp / không numeric |
|---|---|---|---|
| `AICU/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `AICU/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `AICU/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `AICU/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `AICU/Production.csv` | `{"9": 24}` | `[]` | 0 / 0 / 0 |
| `AICU/Resources.csv` | `{"7": 166}` | `[]` | 0 / 0 / 0 |
| `AICU/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `Automatoes/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `Automatoes/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `Automatoes/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `Automatoes/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `Automatoes/Production.csv` | `{"9": 23}` | `[]` | 0 / 0 / 0 |
| `Automatoes/Resources.csv` | `{"7": 166}` | `[]` | 0 / 0 / 0 |
| `Automatoes/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `Digilog/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `Digilog/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `Digilog/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `Digilog/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `Digilog/Production.csv` | `{"9": 23}` | `[]` | 0 / 0 / 0 |
| `Digilog/Resources.csv` | `{"7": 166}` | `[]` | 0 / 0 / 0 |
| `Digilog/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/Production.csv` | `{"9": 23}` | `[]` | 0 / 0 / 0 |
| `IUACAAS/Resources.csv` | `{"12": 166}` | `[7, 8, 9, 10, 11]` | 0 / 0 / 0 |
| `IUACAAS/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `Reference/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `Reference/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `Reference/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `Reference/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `Reference/Production.csv` | `{"9": 24}` | `[]` | 0 / 0 / 0 |
| `Reference/Resources.csv` | `{"7": 166}` | `[]` | 0 / 0 / 0 |
| `Reference/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/CropParameters.csv` | `{"6": 23}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/GreenhouseClimate.csv` | `{"50": 47809}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/GrodanSens.csv` | `{"7": 47809}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/LabAnalysis.csv` | `{"39": 10}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/Production.csv` | `{"9": 23}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/Resources.csv` | `{"7": 166}` | `[]` | 0 / 0 / 0 |
| `TheAutomators/TomQuality.csv` | `{"8": 8}` | `[]` | 0 / 0 / 0 |
| `Weather/Weather.csv` | `{"11": 47809}` | `[]` | 0 / 0 / 0 |

## Token rỗng của các cột phục vụ BR

Chỉ liệt kê cột có token rỗng/NaN/NA/N/A; cột không liệt kê có số đếm 0. Đây không phải accepted coverage: giá trị 0, suspect, rule, setpoint off-mode và basis chưa được đánh giá. Manifest giữ count theo index cho tất cả cột.

| File | Cột | Số token rỗng | Tổng dòng |
|---|---|---:|---:|
| `AICU/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `AICU/GreenhouseClimate.csv` | `EC_drain_PC` | 71 | 47809 |
| `AICU/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `AICU/GreenhouseClimate.csv` | `co2_sp` | 41125 | 47809 |
| `AICU/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `AICU/GreenhouseClimate.csv` | `t_heat_sp` | 33319 | 47809 |
| `AICU/GreenhouseClimate.csv` | `t_vent_sp` | 27810 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `EC_drain_PC` | 71 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `co2_sp` | 754 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `t_heat_sp` | 706 | 47809 |
| `Automatoes/GreenhouseClimate.csv` | `t_vent_sp` | 12576 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `EC_drain_PC` | 62 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `co2_sp` | 516 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `t_heat_sp` | 517 | 47809 |
| `Digilog/GreenhouseClimate.csv` | `t_vent_sp` | 522 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `EC_drain_PC` | 71 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `co2_sp` | 2441 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `t_heat_sp` | 2186 | 47809 |
| `IUACAAS/GreenhouseClimate.csv` | `t_vent_sp` | 2204 | 47809 |
| `Reference/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `Reference/GreenhouseClimate.csv` | `EC_drain_PC` | 71 | 47809 |
| `Reference/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `Reference/GreenhouseClimate.csv` | `co2_sp` | 1522 | 47809 |
| `Reference/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `Reference/GreenhouseClimate.csv` | `t_heat_sp` | 1946 | 47809 |
| `Reference/GreenhouseClimate.csv` | `t_vent_sp` | 1960 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `CO2air` | 71 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `EC_drain_PC` | 71 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `Tair` | 71 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `co2_sp` | 644 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `pH_drain_PC` | 71 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `t_heat_sp` | 540 | 47809 |
| `TheAutomators/GreenhouseClimate.csv` | `t_vent_sp` | 542 | 47809 |

## Điểm chuyển tiếp cho T03/T06

- Climate/Grodan/Weather có grain quan sát theo timestamp; Resources là tổng ngày; Production là record thu hoạch thưa. Không suy coverage harvest bằng số ngày có record.
- Đối chiếu timestamp ngoài cửa sổ vụ ở Production với nguồn gốc; không sửa năm hoặc quarantine bằng inventory. T03 quyết định ngữ nghĩa; T06 giữ source file/dòng.
- Header rỗng dư, tab/whitespace và row width lệch cần adapter có version ở T06. Không coi header cũ trong docs/ref là dictionary hiện hành.
- IUACAAS Resources có 12 cột (5 cột cuối toàn rỗng); Reference TomQuality có 7 trường header nhưng 8 trường mỗi dòng dữ liệu. Các khác biệt này được giữ nguyên; T06 phải quyết định adapter có bằng chứng, không silently truncate.
- Basis nước/nhiệt/điện/Production, timezone/DST, off-mode và biểu giá chưa được xác minh bởi phép kiểm kê này. Không công bố KPI hoặc xếp hạng từ inventory.

## Tái tạo

Theo [hướng dẫn dataset](dataset-snapshot.md): chạy lệnh inventory trên snapshot đã khóa; output Markdown ổn định, không chứa acquisition time hoặc đường dẫn máy. Manifest giữ acquisition UTC và provenance của lần publish đầu tiên.
