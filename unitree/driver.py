"""
UnitreeDriver — 宇树机器人 DDS↔ZMQ 桥接驱动
=============================================
完整模拟宇树机器人的 DDS 接口:
  - 订阅 rt/lowcmd (控制脚本发来的关节指令)
  - 发布 rt/lowstate (引擎状态回传给控制脚本)
  - MotionSwitcher / Sport RPC 服务 (含 Dance1/Dance2 等实际动作执行)
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

G1_MOTOR_NAMES = [
    # 左腿 (0-5)
    "left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
    "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
    # 右腿 (6-11)
    "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
    "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint",
    # 腰 (12-14)
    "waist_yaw_joint", "waist_roll_joint", "waist_pitch_joint",
    # 左臂 (15-21)
    "left_shoulder_pitch_joint", "left_shoulder_roll_joint", "left_shoulder_yaw_joint",
    "left_elbow_joint", "left_wrist_roll_joint", "left_wrist_pitch_joint", "left_wrist_yaw_joint",
    # 右臂 (22-28)
    "right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
    "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint", "right_wrist_yaw_joint",
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


class SportMotionServer:
    """Sport RPC 服务 — 接收高级动作指令并通过 MotionExecutor 执行"""

    def __init__(self):
        self._server = None
        self._executor = None

    def set_executor(self, executor):
        """注入 MotionExecutor 引用"""
        self._executor = executor

    def start(self):
        from unitree_sdk2py.rpc.server import Server
        from unitree_sdk2py.go2.sport.sport_api import (
            SPORT_SERVICE_NAME,
            SPORT_API_VERSION,
            SPORT_API_ID_DAMP,
            SPORT_API_ID_BALANCESTAND,
            SPORT_API_ID_STOPMOVE,
            SPORT_API_ID_STANDUP,
            SPORT_API_ID_STANDDOWN,
            SPORT_API_ID_RECOVERYSTAND,
            SPORT_API_ID_HELLO,
            SPORT_API_ID_STRETCH,
            SPORT_API_ID_CONTENT,
            SPORT_API_ID_DANCE1,
            SPORT_API_ID_DANCE2,
            SPORT_API_ID_HEART,
            SPORT_API_ID_EULER,
            SPORT_API_ID_MOVE,
            SPORT_API_ID_SIT,
            SPORT_API_ID_RISESIT,
            SPORT_API_ID_SPEEDLEVEL,
            SPORT_API_ID_SWITCHJOYSTICK,
            SPORT_API_ID_POSE,
            SPORT_API_ID_SCRAPE,
            SPORT_API_ID_FRONTFLIP,
            SPORT_API_ID_FRONTJUMP,
            SPORT_API_ID_FRONTPOUNCE,
        )

        self._server = Server(SPORT_SERVICE_NAME)
        self._server._SetApiVersion(SPORT_API_VERSION)

        # 有实际动作的命令
        motion_apis = [
            SPORT_API_ID_STANDUP,
            SPORT_API_ID_STANDDOWN,
            SPORT_API_ID_RECOVERYSTAND,
            SPORT_API_ID_BALANCESTAND,
            SPORT_API_ID_HELLO,
            SPORT_API_ID_STRETCH,
            SPORT_API_ID_CONTENT,
            SPORT_API_ID_DANCE1,
            SPORT_API_ID_DANCE2,
            SPORT_API_ID_HEART,
        ]
        for api_id in motion_apis:
            self._server._RegistHandler(api_id, self._make_motion_handler(api_id), False)

        # 简单应答的命令 (无实际动作)
        noop_apis = [
            SPORT_API_ID_DAMP,
            SPORT_API_ID_STOPMOVE,
            SPORT_API_ID_EULER,
            SPORT_API_ID_MOVE,
            SPORT_API_ID_SIT,
            SPORT_API_ID_RISESIT,
            SPORT_API_ID_SPEEDLEVEL,
            SPORT_API_ID_SWITCHJOYSTICK,
            SPORT_API_ID_POSE,
            SPORT_API_ID_SCRAPE,
            SPORT_API_ID_FRONTFLIP,
            SPORT_API_ID_FRONTJUMP,
            SPORT_API_ID_FRONTPOUNCE,
        ]
        for api_id in noop_apis:
            self._server._RegistHandler(api_id, self._noop, False)

        self._server.Start()
        logger.info("Sport RPC 服务已启动 (支持 Dance1/Dance2/Hello 等动作)")

    def _make_motion_handler(self, api_id):
        """为每个 API ID 创建独立的 handler 闭包"""
        def handler(parameter):
            return self._execute_motion(api_id)
        return handler

    def _execute_motion(self, api_id):
        """触发 MotionExecutor 播放对应动作"""
        if self._executor is None:
            logger.warning("MotionExecutor 未初始化")
            return 1, ""

        from .sport_motions import get_motion_for_api
        sequence = get_motion_for_api(api_id)
        if sequence is None:
            logger.warning(f"未找到 API {api_id} 对应的动作序列")
            return 1, ""

        logger.info(f"Sport 指令: {sequence.name} (api_id={api_id})")
        self._executor.play(sequence)
        return 0, ""

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

        # 根据关节数自动识别机器人类型
        n_joints = len(robot_info.joints) if robot_info.joints else 12
        if n_joints >= 29:
            self._robot_type = "g1"
            self._motor_names = robot_info.joints if robot_info.joints else G1_MOTOR_NAMES
            self._num_motors = 29
        else:
            self._robot_type = "go2"
            self._motor_names = robot_info.joints if robot_info.joints else GO2_MOTOR_NAMES
            self._num_motors = 12

        self._mock_msc = MockMotionSwitcherServer()
        self._sport_server = SportMotionServer()
        self._motion_executor = None
        self._tick_counter = 0

    def start(self):
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
            )
            import os
            dds_iface = os.environ.get("UNITREE_DDS_IFACE", "lo")
            ChannelFactoryInitialize(0, dds_iface)
            logger.info(f"DDS 初始化: domain=0, interface={dds_iface}")

            if self._robot_type == "g1":
                from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
                from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
                self._LowState_factory = unitree_hg_msg_dds__LowState_
            else:
                from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
                from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
                self._LowState_factory = unitree_go_msg_dds__LowState_

            # Go2 专有的 Sport 动作服务
            if self._robot_type == "go2":
                from .sport_motions import MotionExecutor
                self._motion_executor = MotionExecutor(self._on_motion_cmd)
                self._sport_server.set_executor(self._motion_executor)
                self._sport_server.start()

            self._mock_msc.start()

            self._lowcmd_sub = ChannelSubscriber("rt/lowcmd", LowCmd_)
            self._lowcmd_sub.Init(self._on_dds_lowcmd, 10)

            self._lowstate_pub = ChannelPublisher("rt/lowstate", LowState_)
            self._lowstate_pub.Init()

            self._running = True
            logger.info(f"[{self.robot_id}] 宇树 {self._robot_type.upper()} DDS 驱动已启动 (电机: {self._num_motors})")
        except ImportError as e:
            raise RuntimeError(f"需要安装 unitree_sdk2py: {e}")

    def stop(self):
        if self._motion_executor:
            self._motion_executor.stop()
        self._running = False
        logger.info(f"[{self.robot_id}] 宇树 DDS 驱动已停止")

    def _on_motion_cmd(self, pose: List[float], kp: float, kd: float):
        """MotionExecutor 的回调: 直接生成 JointCommand (绕过 DDS 避免回路)"""
        n = min(len(pose), len(self._motor_names))
        motors = []
        for i in range(n):
            motors.append(MotorCommand(
                name=self._motor_names[i],
                q=pose[i],
                dq=0.0,
                kp=kp,
                kd=kd,
                tau=0.0,
            ))
        self._pending_cmd = JointCommand(robot_id=self.robot_id, motors=motors)

    def _on_dds_lowcmd(self, msg):
        """DDS 回调: 控制脚本发来 LowCmd → 转为 JointCommand"""
        # 用户脚本手动发指令时，停止正在播放的 Sport 动作
        if self._motion_executor and self._motion_executor.is_playing:
            # 检查是否来自 MotionExecutor 自己 (避免死循环)
            # Note: MotionExecutor 通过 _publish_lowcmd 发送，也会触发本回调
            # 简单方案: 不在这里停止，让 Sport 动作自然完成
            pass

        n = min(self._num_motors, len(msg.motor_cmd))
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
        """引擎状态 → DDS rt/lowstate + MotionExecutor 状态更新"""
        if not self._lowstate_pub:
            return

        low_state = self._LowState_factory()
        self._tick_counter += 1
        low_state.tick = self._tick_counter
        joint_positions = [0.0] * self._num_motors
        for motor in state.motors:
            try:
                idx = self._motor_names.index(motor.name)
            except ValueError:
                continue
            low_state.motor_state[idx].q = motor.q
            low_state.motor_state[idx].dq = motor.dq
            low_state.motor_state[idx].tau_est = motor.tau
            if idx < self._num_motors:
                joint_positions[idx] = motor.q

        self._lowstate_pub.Write(low_state)

        if self._motion_executor:
            self._motion_executor.update_state(joint_positions)
