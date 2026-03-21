# NexusRobotDrivers

NexusEngine 厂商机器人驱动仓库。

## 结构

```
NexusRobotDrivers/
├── manifest.json     # robot_name → driver 映射
└── unitree/          # 宇树全系列 (Go2/B2/G1/H1)
    ├── driver.py     # UnitreeDriver
    └── requirements.txt
```

## 添加驱动

1. 新建厂商目录，在 `driver.py` 中继承 `RobotDriver`
2. 在 `manifest.json` 添加 `robot_name → driver` 映射
3. 提交到仓库

NexusRobotBridge 会自动从此仓库拉取驱动。
