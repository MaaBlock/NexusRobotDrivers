"""
UnitreeDriver — 宇树机器人 DDS↔ZMQ 桥接驱动
=============================================
完整模拟宇树机器人的 DDS 接口:
  - 订阅 rt/lowcmd (控制脚本发来的关节指令)
  - 发布 rt/lowstate (引擎状态回传给控制脚本)
  - 模拟 MotionSwitcher / Sport RPC 服务 (官方脚本初始化需要)
"""
import json
import time
import logging
import threading
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


class MockMotionSwitcherServer:
    """模拟 MotionSwitcher RPC 服务"""
    def __init__(self):
        self._server = None

    def start(self):
        from unitree_sdk2py.rpc.server import Server
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_api import (
            MOTION_SWITCHER_SERVICE_NAME,
            MOTION_SWITCHER_API_VERSION,
            MOTION_SWITCHER_API_ID_CHECK_MODE,
            MOTION_SWITCHER_API_ID_SELECT_MODE,
            MOTION_SWITCHER_API_ID_RELEASE_MODE,
            MOTION_SWITCHER_API_ID_SET_SILENT,
            MOTION_SWITCHER_API_ID_GET_SILENT,
        )

        self._server = Server(MOTION_SWITCHER_SERVICE_NAME)
        self._server._SetApiVersion(MOTION_SWITCHER_API_VERSION)
        self._server._RegistHandler(MOTION_SWITCHER_API_ID_CHECK_MODE, self._check_mode, False)
        self._server._RegistHandler(MOTION_SWITCHER_API_ID_SELECT_MODE, self._noop, False)
        self._server._RegistHandler(MOTION_SWITCHER_API_ID_RELEASE_MODE, self._noop, False)
        self._server._RegistHandler(MOTION_SWITCHER_API_ID_SET_SILENT, self._noop, False)
        self._server._RegistHandler(MOTION_SWITCHER_API_ID_GET_SILENT, self._noop, False)
        self._server.Start()
        logger.info("MotionSwitcher RPC 模拟服务已启动")

    def _check_mode(self, parameter):
        return 0, json.dumps({"name": ""})

    def _noop(self, parameter):
        return 0, ""


class MockSportServer:
    """模拟 Sport RPC 服务"""
    def __init__(self):
        self._server = None

    def start(self):
        from unitree_sdk2py.rpc.server import Server
        from unitree_sdk2py.go2.sport.sport_api import (
            SPORT_SERVICE_NAME,
            SPORT_API_VERSION,
            SPORT_API_ID_STANDDOWN,
            SPORT_API_ID_STANDUP,
            SPORT_API_ID_BALANCESTAND,
            SPORT_API_ID_STOPMOVE,
            SPORT_API_ID_RECOVERYSTAND,
            SPORT_API_ID_DAMP,
        )

        self._server = Server(SPORT_SERVICE_NAME)
        self._server._SetApiVersion(SPORT_API_VERSION)
        self._server._RegistHandler(SPORT_API_ID_STANDDOWN, self._noop, False)
        self._server._RegistHandler(SPORT_API_ID_STANDUP, self._noop, False)
        self._server._RegistHandler(SPORT_API_ID_BALANCESTAND, self._noop, False)
        self._server._RegistHandler(SPORT_API_ID_STOPMOVE, self._noop, False)
        self._server._RegistHandler(SPORT_API_ID_RECOVERYSTAND, self._noop, False)
        self._server._RegistHandler(SPORT_API_ID_DAMP, self._noop, False)
        self._server.Start()
        logger.info("Sport RPC 模拟服务已启动")

    def _noop(self, parameter):
        return 0, ""


class UnitreeDriver(RobotDriver):
    """宇树 DDS↔ZMQ 桥接驱动: 控制脚本(DDS) ←→ NexusEngine(ZMQ)"""
    name = "unitree"

    def __init__(self, robot_id: str, robot_info: RobotInfo):
        super().__init__(robot_id, robot_info)
        self._lowcmd_sub = None
        self._lowstate_pub = None
        self._pending_cmd: Optional[JointCommand] = None
        self._motor_names: List[str] = robot_info.joints if robot_info.joints else GO2_MOTOR_NAMES
        self._mock_msc = MockMotionSwitcherServer()
        self._mock_sport = MockSportServer()

    def start(self):
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
            )
            from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_

            ChannelFactoryInitialize(1, "eth0")

            self._mock_msc.start()
            self._mock_sport.start()

            self._lowcmd_sub = ChannelSubscriber("rt/lowcmd", LowCmd_)
            self._lowcmd_sub.Init(self._on_dds_lowcmd, 10)

            self._lowstate_pub = ChannelPublisher("rt/lowstate", LowState_)
            self._lowstate_pub.Init()

            self._LowState_factory = unitree_go_msg_dds__LowState_
            self._running = True
            logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已启动 (电机: {len(self._motor_names)}, RPC 模拟已开启)")
        except ImportError as e:
            raise RuntimeError(f"需要安装 unitree_sdk2py: {e}")

    def stop(self):
        self._running = False
        logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已停止")

    def _on_dds_lowcmd(self, msg):
        """DDS 回调: 控制脚本发来 LowCmd → 转为 JointCommand"""
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
        pass

    def recv_from_vendor(self) -> Optional[JointCommand]:
        """Bridge 轮询: 取出 DDS 收到的 LowCmd"""
        cmd = self._pending_cmd
        self._pending_cmd = None
        return cmd

    def on_engine_state(self, state: JointState):
        """引擎状态 → DDS rt/lowstate"""
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
