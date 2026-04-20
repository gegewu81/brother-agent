# Brother Agent — 离线多Agent数据镜像

> 多台独立运行的 Hermes Agent，碰面时互相镜像数据，各管各的，零合并零冲突。

## 场景

```
Node A: 独立跑任务 ──── 断开 ──── 独立跑任务
Node B: 独立跑任务 ──── 断开 ──── 独立跑任务
                          ↓ 连通时
                    ha_sync.py sync <name>（一次）
                          ↓ 又断开
                    各干各的，下次碰面再同步
```

类似Git：离线commit，碰面时push/pull。

## 核心思路

**不合并，只镜像。** 每个节点在 `~/.hermes/brothers/` 下为每个brother建独立子目录，存对方的只读镜像。

```
~/.hermes/brothers/
  ├── nodes.yaml              ← 节点注册表
  ├── <name>/                 ← 某个brother的只读镜像
  │   ├── sessions/
  │   ├── state.db            ← FTS5索引
  │   ├── memory_store.db     ← HRR向量
  │   ├── MEMORY.md / USER.md
  │   └── skills/
  └── <name2>/                ← 其他节点
      └── ...
```

## 命名机制

- `ha_sync.py add <name> --host <host>` 注册节点
- 本地视角：对方叫注册时的 `<name>`
- 对方视角：自己叫 `<本地hostname>`（自动）
- 两侧名字可以不同，各管各的目录

## 命令

```bash
ha_sync.py add <name> --host <host> [--desc <描述>]  # 注册
ha_sync.py sync <name>                                # 同步指定brother
ha_sync.py sync --all                                 # 同步所有
ha_sync.py status [<name>]                            # 状态
ha_sync.py list                                       # 列出brother
ha_sync.py remove <name>                              # 删除
```

## 同步流程

```
ha_sync.py sync <name>

[0] SSH连通检测

[1] 推送本地 → 对方:~/.hermes/brothers/<本地hostname>/
    rsync sessions/
    rsync state.db
    conn.backup()快照 → rsync memory_store.db
    rsync MEMORY.md / USER.md
    rsync skills/

[2] 拉取对方 → 本地:~/.hermes/brothers/<name>/
    rsync sessions/
    rsync state.db
    对方端snapshot → rsync memory_store.db
    rsync MEMORY.md / USER.md
    rsync skills/
```

## 查询brother数据

通过 Hermes 的 `brother-agent` skill，用 `execute_code` 直连 SQLite：

```python
from pathlib import Path
db = Path.home() / ".hermes" / "brothers" / "<name>" / "state.db"
# 或 memory_store.db，同样的模式
# FTS5全文搜索 / LIKE模糊搜索 / entity关联查询
```

支持：session全文搜索、memory fact查询、entity关联推理、MEMORY.md读取。

## 文件结构

```
brother-agent/
  ├── README.md              ← 本文件
  ├── docs/
  │   ├── design.md          ← 详细设计文档
  │   └── SKILL.md           ← Hermes skill定义（查询模板）
  └── scripts/
      ├── ha_sync.py         ← 主脚本
      └── ha_snapshot.py     ← 远程memory快照
```

## 快速开始

```bash
# 克隆
git clone https://github.com/gegewu81/brother-agent.git ~/brother-agent

# 注册节点
python3 ~/brother-agent/scripts/ha_sync.py add pi --host pi --desc "树莓派4B"

# 同步
python3 ~/brother-agent/scripts/ha_sync.py sync pi

# 可选：定时同步（每小时尝试，不通就跳过）
# crontab: 0 * * * * python3 ~/brother-agent/scripts/ha_sync.py sync --all --quiet
```

## 设计决策（踩过的坑）

| # | 决策 | 原因 |
|---|------|------|
| 1 | 不合并DB，只镜像 | SQLite双向merge是分布式经典难题 |
| 2 | 不用export→JSON同步memory | 丢fact_entities关联和hrr_vector向量 |
| 3 | 不做主备/epoch/heartbeat | 场景不是故障转移，不需要 |
| 4 | state.db直接rsync | Hermes的派生索引层，不需要rebuild |
| 5 | conn.backup()快照memory | 并发安全，不锁表 |
| 6 | rsync --update | 双向安全，session文件名唯一零冲突 |
| 7 | skills不用--delete | 保护两端自定义skill |

## 与agent-ha的区别

| 维度 | agent-ha (v1-v3) | brother-agent |
|------|-------------------|---------------|
| 架构 | 主备HA | 多活独立 |
| 节点数 | 固定2个 | 不限 |
| 合并 | 需要合并DB | 不合并只镜像 |
| rebuild | 需要 | 不需要 |
| 脚本数 | 4个(1459行) | 2个(~527行) |
| 状态 | 已归档 | 生产使用 |

## 已知限制

1. **网络可达性**: 同步依赖SSH，两端需网络互通（或单向可达）
2. **Schema差异**: 不同Hermes版本的state.db schema不同，查询前先PRAGMA检查
3. **FTS5中文分词有限**: FTS5可能搜不到中文内容，fallback用LIKE
4. **MEMORY.md可能不存在**: 对方没用过memory工具时

## License

MIT
