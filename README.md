# 园区表差分摊 API（Meter Difference Allocation）

纯后端 JSON API：输入总表起止读数与各支路按区间汇总的电量，计算表差
（总表增量 − 支路总和），并以各区间内**各支路电量绝对值之和**为权重，
用定点数最大余数法把表差分摊到既有区间。所有电量以 0.001 kWh 为最小
单位，全程整数定点运算，无浮点误差，同一请求永远得到唯一可复算的结果。

## 运行

```bash
# Docker（端口可用 API_PORT 覆盖，默认 8000）
docker compose up --build api                 # http://localhost:8000
API_PORT=9000 docker compose up --build api   # http://localhost:9000

# 一次性验收服务（跑完即退出，退出码即验收结果）
docker compose up --build --exit-code-from verify --abort-on-container-exit

# 本地开发
pip install -r requirements.txt
uvicorn app.main:app --port 8000
pytest                 # 单元 + API 测试
python verify.py       # 对运行中的 API 做验收（API_BASE_URL 可覆盖）
```

## 接口

`POST /api/v1/settlements/allocate`

```json
{
  "meter": {"start": "100.000", "end": "110.000"},
  "readings": [
    {"branch": "B1", "interval": "I1", "energy": "3.000"},
    {"branch": "B2", "interval": "I1", "energy": "1.000"},
    {"branch": "B1", "interval": "I2", "energy": "2.000"},
    {"branch": "B2", "interval": "I2", "energy": "1.500"},
    {"branch": "B1", "interval": "I3", "energy": "0.500"},
    {"branch": "B2", "interval": "I3", "energy": "0.000"}
  ]
}
```

响应（区间按编号字典序排列，金额均为三位小数字符串）：

```json
{
  "meter_increment": "10.000",
  "branch_total": "8.000",
  "difference": "2.000",
  "check_sum": "2.000",
  "allocations": [
    {"interval": "I1", "branch_total": "4.000", "allocated": "1.000"},
    {"interval": "I2", "branch_total": "3.500", "allocated": "0.875"},
    {"interval": "I3", "branch_total": "0.500", "allocated": "0.125"}
  ]
}
```

- `difference`：原始表差 = 总表增量 − 支路总和。
- `allocated`：每区间分摊值；区间权重 = 该区间内**各支路电量绝对值之和**
  （同一区间正负支路互相抵消时，净额为零但权重仍按绝对电量累计）。
- `check_sum`：分摊后校核和，恒等于 `difference`。

### 可选的支路级明细（detail_level）

请求体可附加 `"detail_level": "branch"`（只接受 `interval` 或 `branch`，
缺省等同 `interval`，非法值返回定位到 `detail_level` 的 422）。此时每个
区间附加按支路编号排序的 `branch_allocations`：服务先把区间分摊值按
**支路电量绝对值**做第二级定点最大余数分配（余数并列按支路编号字典序），
保证各支路 `adjustment` 之和恒等于该区间的 `allocated`：

```json
{"interval": "I1", "branch_total": "4.000", "allocated": "1.000",
 "branch_allocations": [
   {"branch": "B1", "energy": "3.000", "adjustment": "0.750", "adjusted_energy": "3.750"},
   {"branch": "B2", "energy": "1.000", "adjustment": "0.250", "adjusted_energy": "1.250"}
 ]}
```

`energy` 为支路原电量，`adjustment` 为分摊调整量，`adjusted_energy` 为
调整后可入账电量。未传 `detail_level` 时响应字段与上述基础版本完全一致，
不含 `branch_allocations` 键。

### 可选的结算员锁定调整（fixed_adjustments）

当结算员已经依据现场工单确认某条支路读数的调整量时，可在
`detail_level: "branch"` 的请求中附加 `fixed_adjustments`。每项用本请求中
已有的支路和区间唯一定位一条读数，`adjustment` 同样限制为三位小数：

```json
{
  "detail_level": "branch",
  "meter": {"start": "100.000", "end": "110.000"},
  "readings": [
    {"branch": "B1", "interval": "I1", "energy": "3.000"},
    {"branch": "B2", "interval": "I1", "energy": "1.000"},
    {"branch": "B1", "interval": "I2", "energy": "2.000"},
    {"branch": "B2", "interval": "I2", "energy": "1.500"}
  ],
  "fixed_adjustments": [
    {"branch": "B2", "interval": "I1", "adjustment": "0.500"}
  ]
}
```

服务先确认每个锁定项唯一且确实引用本次请求中的读数，然后从原表差中扣除
锁定调整之和；剩余表差只按**未锁定读数**的绝对电量执行区间、支路两级
最大余数分配。最终每个区间的 `allocated` 等于该区间锁定与计算调整之和，
所有支路调整以及区间调整之和仍精确等于原 `difference`。

传入 `fixed_adjustments` 后，每个支路明细增加 `source`：

- `"fixed"`：该读数被锁定，`adjustment` 为请求中的核定值，不参与权重；
- `"calculated"`：该读数未锁定，`adjustment` 来自剩余表差的两级分配。

显式传 `"fixed_adjustments": []` 也要求 `detail_level: "branch"`，此时所有
支路均标记为 `calculated`。完全不传 `fixed_adjustments` 时，即使
`detail_level` 为 `branch`，请求与响应也继续保持原有字段，不出现 `source`。

## 分摊规则（定点最大余数法）

1. 表差、电量全部换算为 0.001 kWh 的整数倍（milliunit）计算。
2. 各区间份额 = `表差 × 权重 / 权重总和`，先**向零截断**。
3. 剩余最小单位按余数绝对值**降序**补齐；并列时按区间编号**字典序升序**。
4. 负表差按同一顺序补**负**单位。
5. 权重总和为零且表差非零：拒绝（`zero_total_weight`）。
6. 存在锁定调整时，锁定支路不参与权重；先对余量执行上述两级分配，再把
   锁定调整加回所属区间。锁定值必须与表差同向（零表差只允许零锁定值），
   且锁定调整绝对值之和不得超过表差绝对值。

## 错误

所有拒绝均返回统一信封且**不产生任何部分结果**：

```json
{"detail": [{"loc": ["body", "readings", 2, "energy"], "msg": "...", "type": "..."}]}
```

| 场景 | type |
| --- | --- |
| 精度越界（超过 3 位小数） | `value_error`（422，loc 定位到具体字段） |
| 读数倒退（end < start） | `value_error`（422，loc 定位到 `meter`） |
| 支路/区间编号为空或全空白 | `value_error`（422，loc 定位到 `readings.<i>.branch` / `readings.<i>.interval`） |
| 请求体出现未知字段（如误拼 `detail_level` 或读数内误拼字段） | `extra_forbidden`（422，loc 定位到该未知字段） |
| `detail_level` 非法取值 | `literal_error`（422，loc 定位到 `detail_level`） |
| 区间集合不一致 | `interval_set_mismatch`（422） |
| 同一支路同一区间重复上报 | `duplicate_reading`（422，loc 含下标） |
| 权重总和为零且表差非零 | `zero_total_weight`（422） |
| 传入 `fixed_adjustments` 但未使用 `detail_level: "branch"` | `fixed_adjustments_requires_branch_detail`（422，loc 定位到 `fixed_adjustments`） |
| 锁定项重复，或未引用本次请求中的支路/区间读数 | `duplicate_fixed_adjustment` / `unknown_fixed_adjustment`（422，loc 含锁定项下标） |
| 锁定调整与表差方向不一致，或绝对值合计超过表差 | `fixed_adjustment_sign_mismatch` / `fixed_adjustment_total_exceeds_difference`（422，loc 定位到具体 `adjustment`） |
| 扣除锁定项后余量非零，但未锁定读数绝对电量合计为零 | `zero_unlocked_weight`（422，loc 定位到具体锁定项或读数） |
