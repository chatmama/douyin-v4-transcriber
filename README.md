# 抖音视频批量转写 + WordPress 发布系统

自动抓取抖音账号视频 → GPU 转写文稿 → 发布到 WordPress。

## 快速启动

```bash
cd ~/douyin_api/versions/v4
bash scripts/start_v4.sh
```

## 查看状态

```bash
python3 modules/monitor.py --report
```

## 文档索引

| 文档 | 说明 |
|------|------|
| [`V4_技术说明.md`](V4_技术说明.md) | 完整技术文档 + 使用方法（含 `.docx` 版本） |
| [`V4_管道可视化.md`](V4_管道可视化.md) | Obsidian 可视化版架构图 |
| [`VERSIONS.md`](VERSIONS.md) | 版本历史 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构设计文档 |
| [`V4_技术说明.docx`](V4_技术说明.docx) | Word 版技术文档 |

## 当前版本

**v4.0**（活跃） — 7 模块微服务架构，全自动管道。

## 目录结构

```
~/douyin_api/
├── output/              # V3 输出（9255篇文稿）
│   ├── 棱镜Talk/
│   ├── 小Lin说/
│   ├── 金灿荣教授/
│   └── 王德峰/
├── versions/
│   └── v4/              # 当前版本
│       ├── modules/     # 7个模块
│       ├── config/      # 配置
│       ├── database/    # SQLite
│       ├── logs/        # 日志
│       ├── output/      # 转写输出
│       └── scripts/     # 启动/状态脚本
├── V4_技术说明.md
└── README.md
```
