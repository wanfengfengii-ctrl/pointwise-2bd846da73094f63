# 修复说明：双谱段交叉录制首槽停滞与半件误领

## 根因

1. **首槽永不推进**（`capture/control.py` 原 25–32 行）
   原实现只有在同一触发凑齐左右两块原板时才放行当前槽，并且还要去对路的
   当前槽里比对同一块板。每路只有一个槽，下一板只有释放当前板后才出现，
   因此左路读完 A、右路读完 B 后两路都停在位置 0，形成死锁；放行还被错误地
   与「双板齐备」耦合。

2. **半件被当成已签发成果**（`archive/result_store.py` 原 77–85 行）
   `read_published` 在正式成果不存在时，会从 `plate_evidence` 的草稿行拼出
   一个 `result_id=None`、右值为 `None` 的 `PublishedResult`。于是 `lookup`
   按 A 能领到只有左值 31 的半件，违反「正式提交前领取为待签」。

3. **半件原板重开后不可用**（`archive/evidence_store.py`）
   原实现把未成对的单板只放在进程内 `_intake` 字典里，重开即丢；表中的
   `first_lane` 没有记录原序号，`read_origin` 只查已配对行，导致已接纳但未配对
   的原板在重开后既读不回，也无法识别同身份/内容冲突。

## 修复

- `capture/control.py`：`step` 只处理本路当前槽——校验、接纳、释放，一次
  推进本路一格。接管失败（校验/接纳）不推进槽位；接纳已提交而释放失败时，
  同一块原板可重试；不再跨路读取或代为释放对方槽位。
- `archive/evidence_store.py`：
  - 表增加 `first_seq`（旧库用 `ALTER TABLE` 迁移），把首块原板原序号落库；
    以 `(batch, lane, 原序号)` 为身份，重投幂等、冲突拒绝。
  - `read_origin` / `read_trigger` 能读回半件原板；`read_trigger` 仍只在
    两板齐备时返回完整对，配对与签发逻辑不变。
  - 半件数据全部在库内，重开后原值与原出处仍可用。
- `archive/result_store.py`：`read_published` 只返回正式提交的成果，草稿只
  是待签，`lookup` 因此返回 `None`。
- 接口、错误类型、事务边界、清单顺序与全部原断言保持不变。

## 实测

```sh
python3 -B check_project.py                       # TEST SUMMARY ... result=PASS（39 项）
python3 -B incident.py                            # {"positions": [1, 1], "lookup_A": null}
python3 -B -m review.reconciliation --sample samples/shift.json
```

- 事故复现入口：两路均推进到位置 1，按 A 领取为 `None`（此前为 `[0,0]` 且能领到 `[31, null]`）。
- 演示：A = 左 31 / 右 47 / 差值 16，出处左 11、右 18；B = 左 29 / 右 43 / 差值 14，
  出处左 14、右 11；B 先签时清单仍为 `A pending, B published`；重开后可重投，
  首次签发被 SQLite 约束拦下后 A 仍待签、历史 B 不变，重投沿用原成果号。
- 新增回归 `tests/test_spectral_handoff.py`：首槽推进与不跨路放行、半件重开
  恢复与幂等重投、跨重开的同原序号冲突拒绝、双板齐备仍须显式签发、
  写入失败无新成果且重开可重投、清单保持登记顺序。
