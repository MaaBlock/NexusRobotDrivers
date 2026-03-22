"""
Go2 跳舞示例 — 使用官方 SportClient 高级接口

通过 SportClient 调用宇树内置的跳舞和特殊动作:
  - Dance1() / Dance2(): 跳舞动作
  - Hello(): 打招呼
  - Stretch(): 伸懒腰
  - Heart(): 比心
  - Content(): 开心

这些动作由宇树官方预先调好，内置平衡控制，不会翻车。

用法:
    python go2_dance_example.py [网卡名]
"""

import time
import sys

from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_go_msg_dds__SportModeState_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
from unitree_sdk2py.go2.sport.sport_client import SportClient


class DanceShow:
    """编排一组官方高级动作的跳舞表演"""

    def __init__(self):
        self.sport_client = SportClient()

        # 动作序列: (名称, 函数, 等待秒数)
        self.choreography = [
            ("站起来",     lambda: self.sport_client.StandUp(),        2),
            ("打招呼",     lambda: self.sport_client.Hello(),          5),
            ("跳舞 1",     lambda: self.sport_client.Dance1(),        10),
            ("伸懒腰",     lambda: self.sport_client.Stretch(),        5),
            ("跳舞 2",     lambda: self.sport_client.Dance2(),        10),
            ("比心",       lambda: self.sport_client.Heart(),          5),
            ("开心",       lambda: self.sport_client.Content(),        5),
            ("跳舞 1",     lambda: self.sport_client.Dance1(),        10),
            ("恢复站立",   lambda: self.sport_client.RecoveryStand(),  3),
        ]

    def Init(self):
        self.sport_client.SetTimeout(10.0)
        self.sport_client.Init()

    def Run(self):
        print("\n[DanceShow] 开始表演!\n")

        for i, (name, action, wait) in enumerate(self.choreography):
            print(f"  [{i+1}/{len(self.choreography)}] {name} ...")
            ret = action()
            if ret != 0:
                print(f"    (返回码: {ret})")
            time.sleep(wait)

        print("\n[DanceShow] 表演结束!")


if __name__ == '__main__':

    print("=" * 60)
    print("  Go2 跳舞示例 (Dance Demo)")
    print("  使用官方 SportClient 高级动作接口")
    print("=" * 60)
    print("\nWARNING: 请确保机器人周围没有障碍物!")
    input("按 Enter 开始...")

    if len(sys.argv) > 1:
        ChannelFactoryInitialize(0, sys.argv[1])
    else:
        ChannelFactoryInitialize(0)

    show = DanceShow()
    show.Init()
    show.Run()

    time.sleep(1)
    sys.exit(0)
