# 自备机组许可与运行采集

本项目接收公开来源的原文及关联记录。当前样例只给出部分证据，未决状态不等于业务事实不存在。

来源响应由 transport 校验，domain 解释资料，ledger 保留事务与版本与附件，view 展示当前证据和缺口。样例为合成资料，不访问真实网站。

## 资料范围

- 州许可目录：许可编号、机组/设备身份、设施、燃料、容量、获批与适用期间。
- 设备清单：设备编号、型号、容量、安装状态；换型保留旧设备并标注后继。
- 运行申报：试运行与正式运行、适用期间、实测发电量；撤回和迟到申报单独标注。

阶段分为获批、安装、试运行、正式运行四类。撤回、过期、暂停是许可状态，不当作阶段，也不删除许可或设备记录。容量与发电量分列；许可撤回或过期后，许可容量只作为历史值展示，实际发电只认运行申报。设施归属、迟到申报和未关联原文都保留为缺口，不推断为事实。

## 使用

在项目根目录执行：

    python -m unittest discover -s tests -v
    python -m compileall -q capture_service tests
    python -m capture_service.cli inspect data/normal.json
    python -m capture_service.cli inspect data/complete.json
    python -m capture_service.cli inspect data/lifecycle.json
    python -m capture_service.cli inspect data/ambiguous.json
    python -m capture_service.cli inspect data/catalog.json
    python -m capture_service.cli inspect data/attachment.json
    python -m capture_service.cli inspect data/attachment-url.json

Dockerfile 提供 Linux 环境；docker build -t onsite-generation . 构建，docker run --rm onsite-generation 运行样例。
