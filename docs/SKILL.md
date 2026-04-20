---
name: brother-agent
description: 查询兄弟节点的数据镜像（sessions、memory、MEMORY.md）。当用户提到"brother"、"另一台机器"、"Pi上的"、"别的节点"时加载。
version: 1.0.0
category: devops
---

# Brother Agent — 查询兄弟节点数据

## 什么时候加载

- 用户提到 "brother"、"兄弟节点"、"另一台机器"、"Pi上的数据"、"别的agent"
- 需要查找本地没有但可能在其他节点上存在的信息

## 架构

多个独立Hermes Agent各自运行，碰面时通过 `ha_sync.py sync` 镜像数据到 `~/.hermes/brothers/<name>/`。

所有brother数据都是**只读镜像**，不需要也不应该修改。

## 数据位置

```
~/.hermes/brothers/
  ├── nodes.yaml           ← 节点注册表
  ├── <name>/              ← 每个brother独立子目录
  │   ├── sessions/        ← session JSON文件
  │   ├── state.db         ← 完整的state.db（FTS5搜索）
  │   ├── memory_store.db  ← 完整的memory_store.db
  │   ├── MEMORY.md        ← 对方的MEMORY.md
  │   ├── USER.md          ← 对方的USER.md
  │   └── skills/          ← 对方的skills目录
  └── ...
```

## 查询方法

所有查询通过 `execute_code` 直连SQLite，与查本地memory_store.db完全一样的模式。

### 1. 查询brother的session（FTS5全文搜索）

```python
import sqlite3
from pathlib import Path

brother_name = "pi"  # 替换为实际名字
db = Path.home() / ".hermes" / "brothers" / brother_name / "state.db"

if not db.exists():
    print(f"brother '{brother_name}' 数据不存在，请先执行 ha_sync.py sync {brother_name}")
else:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # FTS5搜索
    keyword = "要搜索的关键词"
    rows = conn.execute("""
        SELECT m.session_id, m.content, s.source, s.started_at, s.model
        FROM messages_fts f
        JOIN messages m ON f.rowid = m.id
        JOIN sessions s ON m.session_id = s.id
        WHERE messages_fts MATCH ?
        ORDER BY s.started_at DESC LIMIT 10
    """, (keyword,)).fetchall()
    for r in rows:
        from datetime import datetime
        ts = datetime.fromtimestamp(r['started_at']).strftime('%Y-%m-%d %H:%M')
        print(f"[{ts}] [{r['source']}] {r['content'][:200]}")
    conn.close()
```

### 2. 查询brother的memory（fact_store）

```python
import sqlite3
from pathlib import Path

brother_name = "pi"
db = Path.home() / ".hermes" / "brothers" / brother_name / "memory_store.db"

if not db.exists():
    print(f"brother '{brother_name}' memory不存在")
else:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    keyword = "要搜索的关键词"
    rows = conn.execute("""
        SELECT f.content, f.category, f.tags, f.trust_score
        FROM facts f
        WHERE f.content LIKE ?
        ORDER BY f.trust_score DESC LIMIT 10
    """, (f"%{keyword}%",)).fetchall()
    for r in rows:
        print(f"[{r['category']}] trust={r['trust_score']:.2f} {r['content']}")
    conn.close()
```

### 3. 读取brother的MEMORY.md

```python
from pathlib import Path
brother_name = "pi"
p = Path.home() / ".hermes" / "brothers" / brother_name / "MEMORY.md"
if p.exists():
    print(p.read_text())
else:
    print("不存在")
```

### 4. 查询brother的entity及关联fact

```python
import sqlite3
from pathlib import Path

brother_name = "pi"
db = Path.home() / ".hermes" / "brothers" / brother_name / "memory_store.db"

if db.exists():
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    entity_name = "要查的实体名"
    rows = conn.execute("""
        SELECT f.content, f.category, e.name as entity
        FROM facts f
        JOIN fact_entities fe ON f.fact_id = fe.fact_id
        JOIN entities e ON fe.entity_id = e.entity_id
        WHERE e.name LIKE ?
    """, (f"%{entity_name}%",)).fetchall()
    for r in rows:
        print(f"[{r['entity']}] {r['content']}")
    conn.close()
```

## 同步命令

```bash
# 注册新节点
python3 ~/brother-agent/scripts/ha_sync.py add pi --host pi --desc "树莓派4B"

# 同步指定节点
python3 ~/brother-agent/scripts/ha_sync.py sync pi

# 同步所有节点
python3 ~/brother-agent/scripts/ha_sync.py sync --all

# 查看状态
python3 ~/brother-agent/scripts/ha_sync.py status

# 列出所有brother
python3 ~/brother-agent/scripts/ha_sync.py list
```

## 同步设计要点

- memory_store.db用conn.backup()快照(并发安全)，不要直接rsync正在写入的.db
- state.db由Hermes自己维护，直接rsync即可，不需要rebuild
- rsync --update双向安全，session文件名唯一(时间戳+随机ID)零冲突
- skills也用--update，不用--delete(保护两端自定义skill)
- Hermes的atomic_json_write用os.replace()，rsync不会读到半写文件

## 历史教训(来自agent-ha v1-v3)

1. 不要做DB合并——两个独立SQLite双向merge是分布式系统经典难题，简单镜像就好
2. 不要用export→JSON同步memory_store.db——丢fact_entities关联和hrr_vector向量
3. 不要设计角色(primary/standby)/epoch/heartbeat——如果场景不是故障转移就不需要
4. state.db是Hermes的派生索引层，不需要手动rebuild，直接rsync
5. MEMORY.md/USER.md是追加式写入，mtime覆盖会丢数据，镜像到独立目录可避免

## 注意事项

- brother数据是**只读镜像**，不要尝试修改
- 数据可能不是最新的（取决于上次sync时间）
- 查询前先检查文件是否存在，不存在说明还没sync过
- 如果搜索结果为空，建议用户先执行sync
