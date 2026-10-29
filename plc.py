import snap7

from snap7.util import get_real, set_real


class PLCManager:
    """PLC通信管理类"""

    # 内存类型 → 读写方法的映射
    _READ_METHODS = {
        'mb': 'mb_read',
        'db': 'db_read',
        'ib': 'ib_read',
        'qb': 'qb_read',
    }

    _WRITE_METHODS = {
        'mb': 'mb_write',
        'db': 'db_write',
        'qb': 'qb_write',
    }

    def __init__(self):
        self.client = None
        self.is_connected = False

    # ── 连接 / 断开 ──────────────────────────────────────

    def connect(self, ip: str, conn_type: int, rack: int = 0, slot: int = 1):
        """连接到PLC，成功返回 client，失败抛出异常"""
        self.client = snap7.client.Client()
        self.client.set_connection_type(conn_type)
        self.client.connect(ip, rack, slot)
        self.is_connected = True
        return self.client

    def disconnect(self):
        """断开PLC连接"""
        if self.client and self.is_connected:
            try:
                self.client.disconnect()
            finally:
                self.is_connected = False

    # ── 字节读写 ─────────────────────────────────────────

    def _to_byte_data(self, data: int) -> bytes:
        """将 0-255 整数转为单字节 bytes，越界返回 None"""
        if not 0 <= data <= 255:
            return None
        return bytes.fromhex(f"{data:02x}")

    def _write_region(self, client, region: str, offset: int, data: int,
                      db_number: int = 1):
        """通用写入：region 为 'mb' / 'db' / 'qb'"""
        byte_data = self._to_byte_data(data)
        if byte_data is None:
            return
        method_name = self._WRITE_METHODS.get(region)
        if method_name is None:
            raise ValueError(f"不支持的内存区域: {region}")
        write_fn = getattr(client, method_name)
        if region == 'db':
            write_fn(db_number, offset, byte_data)
        else:
            write_fn(offset, 1, byte_data)

    def write_MB(self, client, offset: int, data: int):
        self._write_region(client, 'mb', offset, data)

    def write_VB(self, client, offset: int, data: int):
        self._write_region(client, 'db', offset, data)

    def write_QB(self, client, offset: int, data: int):
        self._write_region(client, 'qb', offset, data)

    def read_memory_byte(self, start: int, size: int,
                         memory_type: str = 'db', db_number: int = 1):
        """通用读取内存字节"""
        try:
            method_name = self._READ_METHODS.get(memory_type)
            if method_name is None:
                raise ValueError(f"不支持的内存类型: {memory_type}")
            read_fn = getattr(self.client, method_name)
            if memory_type == 'db':
                return read_fn(db_number, start, size)
            return read_fn(start, size)
        except Exception:
            return None

    # ── 浮点数读写 ────────────────────────────────────────

    def read_float(self, offset: int, db_number: int = 1):
        """从 DB 块读取一个浮点数"""
        try:
            data = self.client.db_read(db_number, offset, 4)
            return get_real(data, 0)
        except Exception:
            return None

    def write_float(self, offset: int, value: float, db_number: int = 1) -> bool:
        """向 DB 块写入一个浮点数"""
        try:
            data = bytearray(4)
            set_real(data, 0, value)
            self.client.db_write(db_number, offset, data)
            return True
        except Exception:
            return False
