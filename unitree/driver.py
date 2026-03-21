"""
UnitreeDriver — 宇树机器人 DDS 驱动
====================================
通过 unitree_sdk2py 与宇树机器人 DDS 通信。
支持 Go2, B2, G1, H1 等全系列。
"""
import time
import logging
from typing import Optional

from nexus_bridge.drivers.base import RobotDriver
from nexus_bridge.protocol import RobotInfo, JointCommand, JointState, MotorState

logger = logging.getLogger("nexus_bridge.driver.unitree")

# 宇树 Go2 关节索引映射
GO2_JOINT_MAP = {
    "FL_hip_joint": 0,  "FL_thigh_joint": 1,  "FL_calf_joint": 2,
    "FR_hip_joint": 3,  "FR_thigh_joint": 4,  "FR_calf_joint": 5,
    "RL_hip_joint": 6,  "RL_thigh_joint": 7,  "RL_calf_joint": 8,
    "RR_hip_joint": 9,  "RR_thigh_joint": 10, "RR_calf_joint": 11,
}


class UnitreeDriver(RobotDriver):
    """宇树机器人 DDS 驱动"""
    name = "unitree"

    def __init__(self, robot_id: str, robot_info: RobotInfo):
        super().__init__(robot_id, robot_info)
        self._pub = None
        self._sub = None
        self._low_state = None
        self._joint_map = GO2_JOINT_MAP
        self._crc = None

    def start(self):
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
            )
            from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
            from unitree_sdk2py.utils.crc import CRC

            ChannelFactoryInitialize(1, "lo")

            self._pub = ChannelPublisher("rt/lowcmd", LowCmd_)
            self._pub.Init()

            self._sub = ChannelSubscriber("rt/lowstate", LowState_)
            self._sub.Init(self._on_low_state, 10)

            self._cmd_factory = unitree_go_msg_dds__LowCmd_
            self._crc = CRC()
            self._running = True

            logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已启动")
        except ImportError as e:
            raise RuntimeError(f"需要安装 unitree_sdk2py: {e}")

    def stop(self):
        self._running = False
        logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已停止")

    def _on_low_state(self, msg):
        """DDS 回调: LowState → 缓存"""
        self._low_state = msg

    def send_to_vendor(self, cmd: JointCommand):
        """引擎指令 → 宇树 DDS LowCmd"""
        if not self._pub:
            return

        low_cmd = self._cmd_factory()
        low_cmd.head[0] = 0xFE
        low_cmd.head[1] = 0xEF
        low_cmd.level_flag = 0xFF

        for i in range(20):
            low_cmd.motor_cmd[i].mode = 0x01

        for motor in cmd.motors:
            idx = self._joint_map.get(motor.name)
            if idx is not None:
                low_cmd.motor_cmd[idx].q = motor.q
                low_cmd.motor_cmd[idx].dq = motor.dq
                low_cmd.motor_cmd[idx].kp = motor.kp
                low_cmd.motor_cmd[idx].kd = motor.kd
                low_cmd.motor_cmd[idx].tau = motor.tau

        low_cmd.crc = self._crc.Crc(low_cmd)
        self._pub.Write(low_cmd)

    def recv_from_vendor(self) -> Optional[JointState]:
        """宇树 DDS LowState → 引擎状态"""
        ls = self._low_state
        if ls is None:
            return None

        motors = []
        for name, idx in self._joint_map.items():
            ms = ls.motor_state[idx]
            motors.append(MotorState(
                name=name,
                q=float(ms.q),
                dq=float(ms.dq),
                tau=float(ms.tau_est),
            ))

        return JointState(robot_id=self.robot_id, motors=motors)
