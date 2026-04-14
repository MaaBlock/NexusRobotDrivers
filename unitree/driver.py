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

        from sport_motions import get_motion_for_api
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

        # 引擎已支持传递具体的机器人名称
        self._robot_type = robot_info.robot_name
        self._motor_names = robot_info.joints
        self._num_motors = len(self._motor_names)

        self._mock_msc = MockMotionSwitcherServer()
        self._sport_server = SportMotionServer()
        self._motion_executor = None
        self._tick_counter = 0
        self._latest_jpeg_data = []
        self._video_server = None

    def start(self):
        try:
            from unitree_sdk2py.core.channel import (
                ChannelPublisher, ChannelSubscriber, ChannelFactoryInitialize
            )
            import os
            dds_iface = os.environ.get("UNITREE_DDS_IFACE", "lo")
            ChannelFactoryInitialize(0, dds_iface)
            logger.info(f"DDS 初始化: domain=0, interface={dds_iface}")

            is_humanoid = any(x in self._robot_type.lower() for x in ["g1", "h1"])
            
            if is_humanoid:
                from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
                from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
                self._LowState_factory = unitree_hg_msg_dds__LowState_
            else:
                from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
                from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_
                self._LowState_factory = unitree_go_msg_dds__LowState_

            # Go2 专有的 Sport 动作服务
            if "go2" in self._robot_type.lower():
                from sport_motions import MotionExecutor
                self._motion_executor = MotionExecutor(self._on_motion_cmd)
                self._sport_server.set_executor(self._motion_executor)
                self._sport_server.start()

            self._mock_msc.start()

            self._lowcmd_sub = ChannelSubscriber("rt/lowcmd", LowCmd_)
            self._lowcmd_sub.Init(self._on_dds_lowcmd, 10)

            self._lowstate_pub = ChannelPublisher("rt/lowstate", LowState_)
            self._lowstate_pub.Init()

            self._running = True
            
            try:
                from unitree_sdk2py.rpc.server import Server
                self._video_server = Server("videohub")
                
                # [HOTFIX] 修复原生 unitree_sdk2py 官方库中的严重的 dict/set 拼写 Bug
                # 他们在 sdk.__init__ 里写成了 self.__apiBinarySet = {} 导致对它执行 .add 会报错
                setattr(self._video_server, "_Server__apiBinarySet", set())
                
                self._video_server._SetApiVersion("1.0.0.1")
                # 1001 is VIDEO_API_ID_GETIMAGESAMPLE
                self._video_server._RegistBinaryHandler(1001, self._handle_video_rpc, False)
                self._video_server.Start()
                logger.info("Unitree Video Server 'videohub' started via DDS RPC.")
            except ImportError:
                self._video_server = None
                logger.info("Cannot start video server, unitree_sdk2py.rpc missing.")

            logger.info(f"[{self.robot_id}] 宇树 {self._robot_type.upper()} DDS 驱动已启动 (电机: {self._num_motors})")

        except ImportError as e:
            raise RuntimeError(f"需要安装 unitree_sdk2py: {e}")

    def _handle_video_rpc(self, parameter):
        import time
        # 等待首帧图像最多 3 秒，防止客户端启动太快拿到空数据直接 crash
        for _ in range(60):
            if self._latest_jpeg_data:
                return 0, self._latest_jpeg_data
            time.sleep(0.05)
            
        logger.warning("VideoRPC: 获取图像超时 (无最近帧)")
        return 1, []

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

        # IMU: 使用引擎物理仿真的真实数据
        low_state.imu_state.quaternion = state.imu_quaternion
        low_state.imu_state.gyroscope = state.imu_gyroscope

        # 诊断日志（只打印前几次）
        if self._tick_counter <= 3:
            logger.info(f"[诊断] tick={self._tick_counter} motors_recv={len(state.motors)} "
                        f"imu_q={state.imu_quaternion[:4]} imu_g={state.imu_gyroscope[:3]}")
            if state.motors:
                names_recv = [m.name for m in state.motors[:5]]
                names_local = self._motor_names[:5]
                logger.info(f"[诊断] 引擎 actuator 名(前5): {names_recv}")
                logger.info(f"[诊断] 驱动 motor_names(前5): {names_local}")

        matched = 0
        joint_positions = [0.0] * self._num_motors
        for motor in state.motors:
            try:
                idx = self._motor_names.index(motor.name)
            except ValueError:
                if self._tick_counter <= 3:
                    logger.warning(f"[诊断] 名称不匹配: '{motor.name}' 不在 motor_names 中")
                continue
            
            try:
                low_state.motor_state[idx].q = motor.q
                low_state.motor_state[idx].dq = motor.dq
                low_state.motor_state[idx].tau_est = motor.tau
            except IndexError:
                if self._tick_counter <= 3:
                    logger.warning(f"[诊断] 索引越界: '{motor.name}' idx={idx}")
                continue

            if idx < self._num_motors:
                joint_positions[idx] = motor.q
            matched += 1

        if self._tick_counter <= 3:
            logger.info(f"[诊断] 匹配成功: {matched}/{len(state.motors)}")

        self._lowstate_pub.Write(low_state)

        if self._motion_executor:
            self._motion_executor.update_state(joint_positions)

    def on_vision_image(self, width: int, height: int, pixels: bytes):
        """接收并处理 Bridge 转发来的相机数据"""
        if not getattr(self, "_video_server", None):
            return
        
        # 确保数据长度合法 (RGBA8_UNORM)
        if len(pixels) == width * height * 4:
            try:
                import numpy as np
                import cv2
                img_np = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width, 4))
                img_bgr = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
                ret, buffer = cv2.imencode('.jpg', img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ret:
                    self._latest_jpeg_data = buffer.tolist()
                
            except Exception as e:
                logger.error(f"[诊断] 视觉数据处理失败: {e}")
        else:
            logger.warning(f"[诊断] 接收到不匹配的视觉数据大小: {len(pixels)} (expected {width*height*4})")
