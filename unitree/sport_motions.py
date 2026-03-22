"""
Sport 动作关键帧数据 + 运动执行器

定义各高级动作 (Dance1, Dance2, Hello 等) 的关键帧序列，
MotionExecutor 在后台线程中播放并发布 rt/lowcmd。

关节顺序 (12个):
  [0]FR_hip  [1]FR_thigh  [2]FR_calf
  [3]FL_hip  [4]FL_thigh  [5]FL_calf
  [6]RR_hip  [7]RR_thigh  [8]RR_calf
  [9]RL_hip  [10]RL_thigh  [11]RL_calf
"""

import math
import time
import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Dict

logger = logging.getLogger("nexus_bridge.driver.unitree.motions")

# 标准站立姿态
STAND_POSE = [
    0.0, 0.67, -1.3,   # FR
    0.0, 0.67, -1.3,   # FL
    0.0, 0.67, -1.3,   # RR
    0.0, 0.67, -1.3,   # RL
]

# 蹲下姿态
CROUCH_POSE = [
    0.0, 1.3, -2.5,
    0.0, 1.3, -2.5,
    0.0, 1.3, -2.5,
    0.0, 1.3, -2.5,
]


@dataclass
class MotionKeyframe:
    """单个关键帧: 目标姿态 + 到达该姿态的时间"""
    pose: List[float]      # 12 个关节目标角度
    duration_s: float      # 从上一帧过渡到此帧的时间 (秒)


@dataclass
class MotionSequence:
    """动作序列"""
    name: str
    keyframes: List[MotionKeyframe]
    repeat: int = 1        # 重复次数, 0 = 无限


def _make_sinusoidal_frames(base_pose, modifiers, duration_s, steps=50, cycles=1):
    """生成正弦波动作的关键帧序列

    modifiers: dict of {joint_index: (amplitude, phase_offset)}
    """
    dt = duration_s / steps
    frames = []
    for i in range(steps):
        t = i / steps
        phase = 2 * math.pi * cycles * t
        pose = list(base_pose)
        for idx, (amp, phase_off) in modifiers.items():
            pose[idx] += amp * math.sin(phase + phase_off)
        frames.append(MotionKeyframe(pose=pose, duration_s=dt))
    return frames


# ==================== 预定义动作 ====================

def make_standup():
    return MotionSequence("StandUp", [
        MotionKeyframe(pose=list(STAND_POSE), duration_s=1.0),
    ])


def make_standdown():
    return MotionSequence("StandDown", [
        MotionKeyframe(pose=list(CROUCH_POSE), duration_s=1.0),
    ])


def make_recovery_stand():
    return MotionSequence("RecoveryStand", [
        MotionKeyframe(pose=list(STAND_POSE), duration_s=1.5),
    ])


def make_hello():
    """抬前右腿打招呼"""
    # 先站稳
    pose_ready = list(STAND_POSE)
    # 前右腿抬起 (thigh 收小, calf 折叠)
    pose_wave_up = list(STAND_POSE)
    pose_wave_up[1] = 0.0     # FR_thigh 抬起
    pose_wave_up[2] = -0.5    # FR_calf 微弯

    pose_wave_down = list(STAND_POSE)
    pose_wave_down[1] = 0.3
    pose_wave_down[2] = -0.8

    return MotionSequence("Hello", [
        MotionKeyframe(pose=pose_ready, duration_s=0.5),
        MotionKeyframe(pose=pose_wave_up, duration_s=0.4),
        MotionKeyframe(pose=pose_wave_down, duration_s=0.3),
        MotionKeyframe(pose=pose_wave_up, duration_s=0.3),
        MotionKeyframe(pose=pose_wave_down, duration_s=0.3),
        MotionKeyframe(pose=pose_wave_up, duration_s=0.3),
        MotionKeyframe(pose=pose_ready, duration_s=0.5),
    ])


def make_stretch():
    """伸懒腰 — 前腿前伸，后腿后伸"""
    pose_stretch = list(STAND_POSE)
    # 前腿伸直
    pose_stretch[1] = 0.3    # FR_thigh
    pose_stretch[2] = -0.6   # FR_calf
    pose_stretch[4] = 0.3    # FL_thigh
    pose_stretch[5] = -0.6   # FL_calf
    # 后腿微蹲
    pose_stretch[7] = 1.0    # RR_thigh
    pose_stretch[8] = -1.8   # RR_calf
    pose_stretch[10] = 1.0   # RL_thigh
    pose_stretch[11] = -1.8  # RL_calf

    return MotionSequence("Stretch", [
        MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5),
        MotionKeyframe(pose=pose_stretch, duration_s=1.0),
        MotionKeyframe(pose=pose_stretch, duration_s=1.5),  # 保持
        MotionKeyframe(pose=list(STAND_POSE), duration_s=1.0),
    ])


def make_heart():
    """比心 — 两前腿交叉靠拢"""
    pose_heart = list(STAND_POSE)
    pose_heart[0] = 0.3      # FR_hip 内收
    pose_heart[1] = 0.2      # FR_thigh
    pose_heart[2] = -0.5     # FR_calf
    pose_heart[3] = -0.3     # FL_hip 内收
    pose_heart[4] = 0.2      # FL_thigh
    pose_heart[5] = -0.5     # FL_calf

    return MotionSequence("Heart", [
        MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5),
        MotionKeyframe(pose=pose_heart, duration_s=0.8),
        MotionKeyframe(pose=pose_heart, duration_s=2.0),  # 保持
        MotionKeyframe(pose=list(STAND_POSE), duration_s=0.8),
    ])


def make_content():
    """开心 — 小幅快速蹲起"""
    frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            1: (0.06, 0), 2: (-0.07, 0),      # FR
            4: (0.06, 0), 5: (-0.07, 0),      # FL
            7: (0.06, 0), 8: (-0.07, 0),      # RR
            10: (0.06, 0), 11: (-0.07, 0),    # RL
        },
        duration_s=3.0, steps=60, cycles=6,
    )
    frames.insert(0, MotionKeyframe(pose=list(STAND_POSE), duration_s=0.3))
    frames.append(MotionKeyframe(pose=list(STAND_POSE), duration_s=0.3))
    return MotionSequence("Content", frames)


def make_dance1():
    """跳舞1 — 左右摇摆(只动hip) + 微蹲节奏组合"""
    frames = []

    # 阶段1: 站稳
    frames.append(MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5))

    # 阶段2: 左右摇摆 (3个周期, 只动 hip)
    sway_frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            0: (0.1, 0),           # FR_hip
            3: (-0.1, 0),          # FL_hip (反相)
            6: (0.1, 0),           # RR_hip
            9: (-0.1, 0),          # RL_hip
        },
        duration_s=3.0, steps=60, cycles=3,
    )
    frames.extend(sway_frames)

    # 阶段3: 微蹲节奏 (4个周期)
    squat_frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            1: (0.06, 0), 2: (-0.07, 0),
            4: (0.06, 0), 5: (-0.07, 0),
            7: (0.06, 0), 8: (-0.07, 0),
            10: (0.06, 0), 11: (-0.07, 0),
        },
        duration_s=3.0, steps=60, cycles=4,
    )
    frames.extend(squat_frames)

    # 阶段4: 扭胯 (3个周期, 前后 hip 反向)
    twist_frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            0: (0.08, 0),   # FR_hip
            3: (0.08, 0),   # FL_hip 同向
            6: (-0.08, 0),  # RR_hip 反向
            9: (-0.08, 0),  # RL_hip 反向
        },
        duration_s=3.0, steps=60, cycles=3,
    )
    frames.extend(twist_frames)

    # 阶段5: 回到站立
    frames.append(MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5))

    return MotionSequence("Dance1", frames, repeat=1)


def make_dance2():
    """跳舞2 — 波浪 + 对角踏步 + 摇摆组合"""
    frames = []

    frames.append(MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5))

    # 阶段1: 波浪 (四腿依次相位差)
    wave_frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            1: (0.06, 0),              2: (-0.08, math.pi/4),
            4: (0.06, math.pi/2),      5: (-0.08, math.pi/2 + math.pi/4),
            7: (0.06, math.pi),        8: (-0.08, math.pi + math.pi/4),
            10: (0.06, 3*math.pi/2),   11: (-0.08, 3*math.pi/2 + math.pi/4),
        },
        duration_s=4.0, steps=80, cycles=3,
    )
    frames.extend(wave_frames)

    # 阶段2: 对角踏步 (FR+RL vs FL+RR 交替微蹲)
    stamp_frames = []
    dt = 3.0 / 60
    for i in range(60):
        t = i / 60
        phase = 2 * math.pi * 3 * t
        pose = list(STAND_POSE)
        offset_a = 0.05 * max(0, math.sin(phase))
        offset_b = 0.05 * max(0, math.sin(phase + math.pi))
        pose[1] += offset_a;   pose[2] -= offset_a * 1.2
        pose[10] += offset_a;  pose[11] -= offset_a * 1.2
        pose[4] += offset_b;   pose[5] -= offset_b * 1.2
        pose[7] += offset_b;   pose[8] -= offset_b * 1.2
        stamp_frames.append(MotionKeyframe(pose=pose, duration_s=dt))
    frames.extend(stamp_frames)

    # 阶段3: 组合 — 摇摆 + 微蹲同时
    combo_frames = _make_sinusoidal_frames(
        STAND_POSE,
        modifiers={
            0: (0.08, 0), 3: (-0.08, 0), 6: (0.08, 0), 9: (-0.08, 0),  # hip 摇摆
            1: (0.04, math.pi/2), 4: (0.04, math.pi/2),                  # thigh 微蹲
            7: (0.04, math.pi/2), 10: (0.04, math.pi/2),
            2: (-0.05, math.pi/2), 5: (-0.05, math.pi/2),              # calf 配合
            8: (-0.05, math.pi/2), 11: (-0.05, math.pi/2),
        },
        duration_s=3.0, steps=60, cycles=3,
    )
    frames.extend(combo_frames)

    frames.append(MotionKeyframe(pose=list(STAND_POSE), duration_s=0.5))

    return MotionSequence("Dance2", frames, repeat=1)


# 动作注册表
MOTION_REGISTRY: Dict[int, Callable[[], MotionSequence]] = {}


def _register_motions():
    from unitree_sdk2py.go2.sport.sport_api import (
        SPORT_API_ID_STANDUP, SPORT_API_ID_STANDDOWN,
        SPORT_API_ID_RECOVERYSTAND, SPORT_API_ID_BALANCESTAND,
        SPORT_API_ID_HELLO, SPORT_API_ID_STRETCH,
        SPORT_API_ID_CONTENT, SPORT_API_ID_DANCE1, SPORT_API_ID_DANCE2,
        SPORT_API_ID_HEART,
    )

    MOTION_REGISTRY[SPORT_API_ID_STANDUP] = make_standup
    MOTION_REGISTRY[SPORT_API_ID_STANDDOWN] = make_standdown
    MOTION_REGISTRY[SPORT_API_ID_RECOVERYSTAND] = make_recovery_stand
    MOTION_REGISTRY[SPORT_API_ID_BALANCESTAND] = make_standup
    MOTION_REGISTRY[SPORT_API_ID_HELLO] = make_hello
    MOTION_REGISTRY[SPORT_API_ID_STRETCH] = make_stretch
    MOTION_REGISTRY[SPORT_API_ID_CONTENT] = make_content
    MOTION_REGISTRY[SPORT_API_ID_DANCE1] = make_dance1
    MOTION_REGISTRY[SPORT_API_ID_DANCE2] = make_dance2
    MOTION_REGISTRY[SPORT_API_ID_HEART] = make_heart


def get_motion_for_api(api_id: int) -> Optional[MotionSequence]:
    """根据 API ID 获取对应的动作序列"""
    if not MOTION_REGISTRY:
        _register_motions()
    factory = MOTION_REGISTRY.get(api_id)
    if factory:
        return factory()
    return None


class MotionExecutor:
    """后台线程执行器: 播放 MotionSequence 并发布 rt/lowcmd"""

    def __init__(self, publish_cmd_fn: Callable[[List[float], float, float], None]):
        """
        publish_cmd_fn: 发布关节指令的回调
            签名: (pose: List[float], kp: float, kd: float) -> None
        """
        self._publish_cmd = publish_cmd_fn
        self._current_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._current_motion_name = ""
        self._current_state: Optional[List[float]] = None  # 当前关节位置

    @property
    def is_playing(self) -> bool:
        return self._current_thread is not None and self._current_thread.is_alive()

    @property
    def current_motion(self) -> str:
        return self._current_motion_name

    def update_state(self, joint_positions: List[float]):
        """更新当前关节状态 (从 rt/lowstate 回调)"""
        self._current_state = list(joint_positions)

    def play(self, sequence: MotionSequence):
        """播放一个动作序列 (会中断当前正在播放的动作)"""
        self.stop()
        self._stop_event.clear()
        self._current_motion_name = sequence.name
        self._current_thread = threading.Thread(
            target=self._play_thread,
            args=(sequence,),
            name=f"motion_{sequence.name}",
            daemon=True,
        )
        self._current_thread.start()
        logger.info(f"开始播放动作: {sequence.name}")

    def stop(self):
        """停止当前动作"""
        if self.is_playing:
            self._stop_event.set()
            self._current_thread.join(timeout=2.0)
            self._current_motion_name = ""

    def _play_thread(self, sequence: MotionSequence):
        kp = 60.0
        kd = 5.0
        control_rate = 500  # Hz
        dt = 1.0 / control_rate

        repeat_count = 0
        while not self._stop_event.is_set():
            # 获取起始姿态
            current_pose = self._current_state
            if current_pose is None:
                current_pose = list(STAND_POSE)

            for kf in sequence.keyframes:
                if self._stop_event.is_set():
                    return

                target_pose = kf.pose
                num_steps = max(1, int(kf.duration_s * control_rate))

                for step in range(num_steps):
                    if self._stop_event.is_set():
                        return

                    t = (step + 1) / num_steps
                    # smooth ease-in-out
                    t_smooth = t * t * (3 - 2 * t)

                    interpolated = [
                        current_pose[i] + (target_pose[i] - current_pose[i]) * t_smooth
                        for i in range(12)
                    ]

                    self._publish_cmd(interpolated, kp, kd)
                    time.sleep(dt)

                current_pose = list(target_pose)

            repeat_count += 1
            if sequence.repeat > 0 and repeat_count >= sequence.repeat:
                break

        self._current_motion_name = ""
        logger.info(f"动作播放完成: {sequence.name}")
