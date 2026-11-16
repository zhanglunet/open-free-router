# PRD-474/T1：密钥加密存储设计

> PRD：adopted/PRD-474-ofr-encrypted-key-storage.md（issue #2290）
> 借鉴源：freellmapi（MIT）——只借鉴思路，零代码复用。
> 日期：2026-11-17

## §1 加密落盘（T2）

密钥入库即加密，明文永不落盘：

- 加密算法：XOR + SHA-256 派生 keystream（纯 Python 零加密依赖纪律——同 466 Ed25519；偷走文件需要同时偷 keystore 加密钥文件才有用）；
- 密钥文件 `data/keystore.json`（0600 权限）：`{key_name: {ciphertext, nonce, algo}}`；
- 明文只存在于内存（T2），不进日志、不进 trace（T4 钉值）。

## §2 轮换与迁移（T3）

- 轮换：旧 master key → 新 master key，所有密文重新加密；轮换是事务性的（失败回滚到旧存储）；
- 迁移：旧明文存储（registry.yaml api_key 字段）→ 加密 keystore；迁移前检查旧存储可读，迁移后清旧字段；失败回滚；
- 自检：启动时验密钥可用性（解密成功=ok）；密钥访问审计只记元数据（key_name + 操作类型 + 时间），不记密钥原文。

## §3 与现有 proxy.token 存储的关系

proxy.token 是运行时凭证（进程间传递，不落盘）；keystore.json 是持久化存储（落盘加密）。两者互不影响：proxy.token 从 keystore 解密后注入 env（与 PRD-473 零留存启动器衔接）。

## §4 非目标

不做云端密钥管理；不改密钥的使用方式（只改存储层）；不做密钥健康评分。
