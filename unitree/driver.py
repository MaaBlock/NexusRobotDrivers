"""
UnitreeDriver — 宇树机器人 DDS↔ZMQ 桥接驱动
=============================================
订阅 DDS rt/lowcmd (来自控制脚本如 go2_stand_example.py)
转发为 JointCommand 给引擎

接收引擎 JointState
发布为 DDS rt/lowstate 返回给控制脚本
"""
import time
import logging
from typing import Optional, List

from nexus_bridge.drivers.base import RobotDriver
from nexus_bridge.protocol import RobotInfo, JointCommand, JointState, MotorCommand, MotorState

logger = logging.getLogger("nexus_bridge.driver.unitree")

GO2_MOTOR_NAMES = [
    "FR_hip_joint", "FR_thigh_joint", "FR_calf_joint",
    "FL_hip_joint", "FL_thigh_joint", "FL_calf_joint",
    "RR_hip_joint", "RR_thigh_joint", "RR_calf_joint",
    "RL_hip_joint", "RL_thigh_joint", "RL_calf_joint",
]


class UnitreeDriver(RobotDriver):
    """宇树 DDS↔ZMQ 桥接驱动: 控制脚本(DDS) ←→ NexusEngine(ZMQ)"""
    name = "unitree"

    def __init__(self, robot_id: str, robot_info: RobotInfo):
        super().__init__(robot_id, robot_info)
        self._lowcmd_sub = None
        self._lowstate_pub = None
        self._pending_cmd: Optional[JointCommand] = None
        self._motor_names: List[str] = robot_info.joints if robot_info.joints else GO2_MOTOR_NAMES

    def start(self):
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
            )
            from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_

            ChannelFactoryInitialize(1, "eth0")

            self._lowcmd_sub = ChannelSubscriber("rt/lowcmd", LowCmd_)
            self._lowcmd_sub.Init(self._on_dds_lowcmd, 10)

            self._lowstate_pub = ChannelPublisher("rt/lowstate", LowState_)
            self._lowstate_pub.Init()

            self._LowState_factory = unitree_go_msg_dds__LowState_
            self._running = True
            logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已启动 (电机: {len(self._motor_names)})")
        except ImportError as e:
            raise RuntimeError(f"需要安装 unitree_sdk2py: {e}")

    def stop(self):
        self._running = False
        logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已停止")

    def _on_dds_lowcmd(self, msg):
        """DDS 回调: 控制脚本发来 LowCmd → 转为 JointCommand 缓存"""
        n = min(len(self._motor_names), 20)
        motors = []
        for i in range(n):
            mc = msg.motor_cmd[i]
            motors.append(MotorCommand(
                name=self._motor_names[i],
                q=float(mc.q),
                dq=float(mc.dq),
                kp=float(mc.kp),
                kd=float(mc.kd),
                tau=float(mc.tau),
            ))
        self._pending_cmd = JointCommand(robot_id=self.robot_id, motors=motors)

    def send_to_vendor(self, cmd: JointCommand):
        """引擎不会调用这个方向 (引擎→DDS), 留空"""
        pass

    def recv_from_vendor(self) -> Optional[JointCommand]:
        """Bridge 轮询: 取出 DDS 收到的 LowCmd (已转为 JointCommand)"""
        cmd = self._pending_cmd
        self._pending_cmd = None
        return cmd

    def on_engine_state(self, state: JointState):
        """引擎状态 → 发布为 DDS rt/lowstate 返回给控制脚本"""
        if not self._lowstate_pub:
            return

        low_state = self._LowState_factory()
        for motor in state.motors:
            try:
                idx = self._motor_names.index(motor.name)
            except ValueError:
                continue
            low_state.motor_state[idx].q = motor.q
            low_state.motor_state[idx].dq = motor.dq
            low_state.motor_state[idx].tau_est = motor.tau

        self._lowstate_pub.Write(low_state)
