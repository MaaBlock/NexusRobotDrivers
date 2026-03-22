"""
Go2 跳舞示例 — 低级模式 (Low-Level)
====================================
基于官方 go2_stand_example.py 同款模式:
  - DDS rt/lowcmd + rt/lowstate
  - RecurrentThread 500Hz 控制循环
  - 关键帧插值 + 正弦波动作生成

关节顺序 (12个):
  [0]FR_hip  [1]FR_thigh  [2]FR_calf
  [3]FL_hip  [4]FL_thigh  [5]FL_calf
  [6]RR_hip  [7]RR_thigh  [8]RR_calf
  [9]RL_hip  [10]RL_thigh [11]RL_calf

用法:
    python go2_dance_example.py [网卡名]
"""

import time
import sys
import math

from unitree_sdk2py.core.channel import ChannelPublisher, ChannelFactoryInitialize
from unitree_sdk2py.core.channel import ChannelSubscriber
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC
from unitree_sdk2py.utils.thread import RecurrentThread
import unitree_legged_const as go2
from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
from unitree_sdk2py.go2.sport.sport_client import SportClient

# 标准站立姿态
STAND = [0.0, 0.67, -1.3,  0.0, 0.67, -1.3,
         0.0, 0.67, -1.3,  0.0, 0.67, -1.3]

# 蹲下姿态
CROUCH = [0.0, 1.36, -2.65,  0.0, 1.36, -2.65,
          -0.2, 1.36, -2.65,  0.2, 1.36, -2.65]


class DanceMove:
    """一个舞蹈动作段"""
    def __init__(self, name, duration_ticks, move_fn):
        """
        name: 动作名
        duration_ticks: 持续几拍 (每拍 = 2ms, 500拍 = 1秒)
        move_fn: (tick, duration) → [12个目标角度]
        """
        self.name = name
        self.duration = duration_ticks
        self.move_fn = move_fn


def lerp_pose(pose_a, pose_b, t):
    """线性插值"""
    t = max(0, min(1, t))
    # smooth ease-in-out
    t = t * t * (3 - 2 * t)
    return [a + (b - a) * t for a, b in zip(pose_a, pose_b)]


def make_transition(target, duration_ticks=500):
    """过渡到目标姿态"""
    start = [None]  # 用列表包装实现闭包修改
    def fn(tick, dur):
        if start[0] is None:
            return target  # 第一帧会被 Custom 类处理
        return lerp_pose(start[0], target, tick / dur)
    return DanceMove("过渡", duration_ticks, fn)


def make_sway(cycles=3, duration_ticks=1500, hip_amp=0.1):
    """左右摇摆 — 只动 hip 关节"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        offset = hip_amp * math.sin(phase)
        pose = list(STAND)
        pose[0] += offset    # FR_hip
        pose[3] -= offset    # FL_hip (反相)
        pose[6] += offset    # RR_hip
        pose[9] -= offset    # RL_hip
        return pose
    return DanceMove(f"左右摇摆x{cycles}", duration_ticks, fn)


def make_squat_bounce(cycles=4, duration_ticks=2000, amp=0.06):
    """节奏蹲起"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        offset = amp * math.sin(phase)
        pose = list(STAND)
        for leg in range(4):
            pose[leg*3 + 1] += offset        # thigh
            pose[leg*3 + 2] -= offset * 1.2  # calf 配合
        return pose
    return DanceMove(f"蹲起节奏x{cycles}", duration_ticks, fn)


def make_twist(cycles=3, duration_ticks=1500, amp=0.08):
    """扭胯 — 前后 hip 反向"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        offset = amp * math.sin(phase)
        pose = list(STAND)
        pose[0] += offset;  pose[3] += offset    # 前腿同向
        pose[6] -= offset;  pose[9] -= offset    # 后腿反向
        return pose
    return DanceMove(f"扭胯x{cycles}", duration_ticks, fn)


def make_wave(cycles=3, duration_ticks=2000, amp=0.06):
    """波浪 — 四腿依次相位差"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        pose = list(STAND)
        for leg in range(4):
            leg_phase = phase + leg * math.pi / 2
            pose[leg*3 + 1] += amp * math.sin(leg_phase)
            pose[leg*3 + 2] -= amp * 1.2 * math.sin(leg_phase + math.pi/4)
        return pose
    return DanceMove(f"波浪x{cycles}", duration_ticks, fn)


def make_stamp(cycles=3, duration_ticks=1500, amp=0.05):
    """对角踏步 — FR+RL 与 FL+RR 交替"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        pose = list(STAND)
        a = amp * max(0, math.sin(phase))
        b = amp * max(0, math.sin(phase + math.pi))
        # FR + RL
        pose[1] += a;  pose[2] -= a * 1.2
        pose[10] += a; pose[11] -= a * 1.2
        # FL + RR
        pose[4] += b;  pose[5] -= b * 1.2
        pose[7] += b;  pose[8] -= b * 1.2
        return pose
    return DanceMove(f"踏步x{cycles}", duration_ticks, fn)


def make_combo(cycles=3, duration_ticks=1500):
    """组合 — 摇摆 + 蹲起同时"""
    def fn(tick, dur):
        phase = 2 * math.pi * cycles * tick / dur
        pose = list(STAND)
        hip_off = 0.08 * math.sin(phase)
        squat_off = 0.04 * math.sin(phase * 2)
        pose[0] += hip_off;  pose[3] -= hip_off
        pose[6] += hip_off;  pose[9] -= hip_off
        for leg in range(4):
            pose[leg*3 + 1] += squat_off
            pose[leg*3 + 2] -= squat_off * 1.2
        return pose
    return DanceMove(f"组合x{cycles}", duration_ticks, fn)


# 编排舞蹈序列
DANCE_SEQUENCE = [
    # 阶段 0: 蹲下准备 (来自 stand_example 的初始姿态)
    make_transition(CROUCH, 500),
    # 阶段 1: 站起来
    make_transition(STAND, 500),
    # 阶段 2: 左右摇摆
    make_sway(cycles=3, duration_ticks=1500),
    # 阶段 3: 蹲起节奏
    make_squat_bounce(cycles=4, duration_ticks=2000),
    # 阶段 4: 扭胯
    make_twist(cycles=3, duration_ticks=1500),
    # 阶段 5: 波浪
    make_wave(cycles=3, duration_ticks=2000),
    # 阶段 6: 对角踏步
    make_stamp(cycles=4, duration_ticks=2000),
    # 阶段 7: 组合
    make_combo(cycles=4, duration_ticks=2000),
    # 阶段 8: 再来一次摇摆 (大幅)
    make_sway(cycles=4, duration_ticks=2000, hip_amp=0.12),
    # 阶段 9: 蹲起收尾
    make_squat_bounce(cycles=3, duration_ticks=1500),
    # 阶段 10: 回到站立
    make_transition(STAND, 500),
]


class Custom:
    def __init__(self):
        self.Kp = 60.0
        self.Kd = 5.0
        self.dt = 0.002

        self.low_cmd = unitree_go_msg_dds__LowCmd_()
        self.low_state = None

        self.startPos = [0.0] * 12
        self.currentPos = list(STAND)
        self.firstRun = True
        self.done = False

        # 舞蹈状态
        self.phase = -1          # -1 = 初始过渡到蹲下
        self.phase_tick = 0
        self.init_percent = 0.0  # 初始过渡进度
        self.init_duration = 500

        self.crc = CRC()

    def Init(self):
        self.InitLowCmd()

        self.lowcmd_publisher = ChannelPublisher("rt/lowcmd", LowCmd_)
        self.lowcmd_publisher.Init()

        self.lowstate_subscriber = ChannelSubscriber("rt/lowstate", LowState_)
        self.lowstate_subscriber.Init(self.LowStateMessageHandler, 10)

        self.sc = SportClient()
        self.sc.SetTimeout(5.0)
        self.sc.Init()

        self.msc = MotionSwitcherClient()
        self.msc.SetTimeout(5.0)
        self.msc.Init()

        status, result = self.msc.CheckMode()
        while result['name']:
            self.sc.StandDown()
            self.msc.ReleaseMode()
            status, result = self.msc.CheckMode()
            time.sleep(1)

    def Start(self):
        self.lowCmdWriteThreadPtr = RecurrentThread(
            interval=0.002, target=self.LowCmdWrite, name="dancecmd"
        )
        self.lowCmdWriteThreadPtr.Start()

    def InitLowCmd(self):
        self.low_cmd.head[0] = 0xFE
        self.low_cmd.head[1] = 0xEF
        self.low_cmd.level_flag = 0xFF
        self.low_cmd.gpio = 0
        for i in range(20):
            self.low_cmd.motor_cmd[i].mode = 0x01
            self.low_cmd.motor_cmd[i].q = go2.PosStopF
            self.low_cmd.motor_cmd[i].kp = 0
            self.low_cmd.motor_cmd[i].dq = go2.VelStopF
            self.low_cmd.motor_cmd[i].kd = 0
            self.low_cmd.motor_cmd[i].tau = 0

    def LowStateMessageHandler(self, msg: LowState_):
        self.low_state = msg

    def LowCmdWrite(self):
        if self.done:
            return

        # 第一帧: 记录当前关节位置
        if self.firstRun:
            if self.low_state is None:
                return
            for i in range(12):
                self.startPos[i] = self.low_state.motor_state[i].q
            self.currentPos = list(self.startPos)
            self.firstRun = False
            self.phase = -1
            self.init_percent = 0.0
            return

        # 阶段 -1: 从当前位置平滑过渡到第一个舞蹈姿态 (蹲下)
        if self.phase == -1:
            self.init_percent += 1.0 / self.init_duration
            if self.init_percent >= 1.0:
                self.init_percent = 1.0
                self.phase = 0
                self.phase_tick = 0
                self.currentPos = list(CROUCH)
            target = lerp_pose(self.startPos, CROUCH, self.init_percent)
            self._apply_pose(target)
            return

        # 正式舞蹈阶段
        if self.phase >= len(DANCE_SEQUENCE):
            self.done = True
            return

        move = DANCE_SEQUENCE[self.phase]
        self.phase_tick += 1

        if self.phase_tick >= move.duration:
            # 记录当前姿态作为下一阶段的起点
            self.currentPos = move.move_fn(move.duration - 1, move.duration)
            self.phase += 1
            self.phase_tick = 0
            return

        target = move.move_fn(self.phase_tick, move.duration)
        self._apply_pose(target)

    def _apply_pose(self, pose):
        for i in range(12):
            self.low_cmd.motor_cmd[i].q = pose[i]
            self.low_cmd.motor_cmd[i].dq = 0
            self.low_cmd.motor_cmd[i].kp = self.Kp
            self.low_cmd.motor_cmd[i].kd = self.Kd
            self.low_cmd.motor_cmd[i].tau = 0

        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.lowcmd_publisher.Write(self.low_cmd)


if __name__ == '__main__':

    print("=" * 60)
    print("  Go2 跳舞演示 — 低级模式 (Low-Level Dance Demo)")
    print("=" * 60)
    print()
    print("  动作序列:")
    for i, move in enumerate(DANCE_SEQUENCE):
        secs = move.duration * 0.002
        print(f"    [{i+1:2d}] {move.name:12s}  ({secs:.1f}s)")
    total_secs = sum(m.duration for m in DANCE_SEQUENCE) * 0.002
    print(f"\n  总时长: {total_secs:.1f}s")
    print()
    print("WARNING: 请确保机器人周围没有障碍物!")
    input("按 Enter 开始...")

    if len(sys.argv) > 1:
        ChannelFactoryInitialize(0, sys.argv[1])
    else:
        ChannelFactoryInitialize(0)

    custom = Custom()
    custom.Init()
    custom.Start()

    phase_names = ["初始过渡"] + [m.name for m in DANCE_SEQUENCE]
    last_phase = -2

    while True:
        if custom.done:
            print("\n  🎉 跳舞完成!")
            time.sleep(1)
            sys.exit(0)

        current = custom.phase + 1
        if current != last_phase:
            if current < len(phase_names):
                print(f"  ▶ [{current}/{len(DANCE_SEQUENCE)}] {phase_names[current]}")
            last_phase = current

        time.sleep(0.1)
