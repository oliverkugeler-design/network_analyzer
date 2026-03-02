import socket
import time
import numpy as np
from labdevices.core.base_device import BaseDevice
from labdevices.core.workers import AgilentVNAWorker
from labdevices.core.utils import log_message
from labdevices.core.state_models import Agilent_E8358A_StateModel


class Agilent_E8358A(BaseDevice):
# general limitations of this driver:
# - trigger mode: Manual (software triggered scans, no free run)
# - Correction Enable FALSE
# - sweep time AUTO
# - only 'linear frequency' scans (no CW freq)
# - only S-parameter measurements (no smith chart or...)
# - Fixed internal measurement name 'MyTrace'
    TCP_PORT = 5025
    worker_class = AgilentVNAWorker
    state_model_class = Agilent_E8358A_StateModel

    sweep_time = None       # [s]

    # ------------------------------
    # Mandatory functions
    # ------------------------------

    def connect(self):
        # establish connection to device via ethernet.
        try:
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.s.settimeout(5)
            self.s.connect((self.address, self.TCP_PORT))
            time.sleep(0.2)

            # Optional: Full reset of device
            #self.reset()

            # Send default settings
            self.s.sendall(b":STAT:OPER:DEV:ENAB 16;:STAT:OPER:DEV:PTR 16;\n*CLS\n")
            self.check_errors()

            self.connected = True
            log_message(f"{self.name}: connected to {self.address}")
        except Exception as e:
            log_message(f"{self.name}: could not connect to {self.address}")
            raise Exception(f"Could not connect to device at {self.address}: {e}")

    def disconnect(self):
        try:
            if hasattr(self, 's'):
                self.power_off()
                self.s.close()
                log_message(f"{self.name}: device disconnected.")
        except Exception as e:
            log_message(f"{self.name}: error during disconnect: {e}")
        finally:
            self.connected = False

    def read_data(self, data_mode='FDATA'):
        '''
        Main read function: send software trigger -> wait for scan -> read measurement data

        data_mode:
            "FDATA" -> magnitude only (dB)
            "SDATA" -> magnitude (dB) + phase (deg)

        Returns:
            FDATA: ndarray [N, 2]  -> freq, magnitude_dB
            SDATA: ndarray [N, 3]  -> freq, magnitude_dB, phase_deg
        '''
        if data_mode not in ("FDATA", "SDATA"):
            raise ValueError("data_mode must be 'FDATA' or 'SDATA'")

        if not (self.connected and self.sweep_time):
            return np.array([np.nan])
        try:
            self.send_trigger()
            self.wait_for_acquisition_to_finish(timeout_time=self.sweep_time + 1)
            #self.wait_for_acquisition_to_finish__OLD(timeout_time=self.sweep_time + 1)
            
            # Set Data Format
            self.s.sendall(b"FORM REAL,32\n")
            self.s.sendall(b"FORM:BORD NORM\n")
            
            freq = self.get_frequency_axis()

            # Get measurement data
            if data_mode == "FDATA":
                self.s.sendall(b"CALC1:DATA? FDATA\n")
                mag_db = read_binary_block(self.s, dtype=">f4")
                self.s.sendall(b":DISP:WIND1:TRAC1:Y:AUTO\n") # Display: Autoscale Y

                if len(freq) != len(mag_db):
                    raise ValueError("Frequency and FDATA length mismatch")

                return np.column_stack((freq, mag_db))
            
            # SDATA (complex)
            self.s.sendall(b"CALC1:DATA? SDATA\n")
            raw = read_binary_block(self.s, dtype=">f4")
            self.s.sendall(b":DISP:WIND1:TRAC1:Y:AUTO\n") # Display: Autoscale Y

            if raw.size % 2 != 0:
                raise ValueError("SDATA length is not even")

            complex_data = raw[0::2] + 1j * raw[1::2]

            if len(freq) != len(complex_data):
                raise ValueError("Frequency and SDATA length mismatch")

            magnitude_db = 20.0 * np.log10(np.abs(complex_data))
            phase_deg = np.angle(complex_data, deg=True)

            return np.column_stack((freq, magnitude_db, phase_deg))

        except Exception as e:
            log_message(f"{self.name}: error during read: {e}")
            return np.array([np.nan])

    # ------------------------------
    # Configuration functions
    # ------------------------------

    def conf_measurement(self, measurement_type: str, trace_name: str = "MyTrace"):
        """
        Configure measurement type and trace
        measurement_type: 'S11','S21','S12','S22'
        trace_name: name of the measurement trace / display trace
        """
        valid_meas = ['S11','S21','S12','S22']
        if measurement_type not in valid_meas:
            raise ValueError(f"Invalid measurement type '{measurement_type}', must be one of {valid_meas}")

        # Set up window and trace settings (also for local display on the device)
        self.s.sendall(b"CALC1:PAR:DEL:ALL\n")
        self.s.sendall(f"CALC1:PAR:DEF '{trace_name}',{measurement_type}\n".encode())
        self.s.sendall(f"CALC1:PAR:SEL '{trace_name}'\n".encode())
        self.s.sendall(b"DISP:WIND1:STAT?\n")
        if recv_line(self.s).strip() == "0":
            self.s.sendall(b"DISP:WIND1:STAT ON\n")
        self.s.sendall(f"DISP:WIND1:TRAC1:FEED '{trace_name}'\n".encode())

        # Fixed settings
        self.s.sendall(b"SENS1:SWE:TYPE LIN\n")       # linear frequency sweep
        self.s.sendall(b"SENS1:CORR:STAT OFF\n")      # corrections off
        self.s.sendall(b"TRIG:SOUR MAN\n")            # manual trigger

        self.power_on()

        self.check_errors()

    def conf_frequency(self, start_freq: float = 1e9, stop_freq: float = 2e9):
        """
        Set frequency range [Hz]
        """
        self.s.sendall(f"SENS1:FREQ:START {start_freq}\n".encode())
        self.s.sendall(f"SENS1:FREQ:STOP {stop_freq}\n".encode())
        self.check_errors()

    def conf_bandwidth(self, if_bw: float = 1000):
        """
        Set IF bandwidth [Hz]
        possible values: 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300,
            500, 700, 1k, 1.5k, 2k, 3k, 5k, 7k, 10k, 15k, 20k, 30k, 35k, 40k
        """
        VALID_IFBW = {
                1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000,
                1500, 2000, 3000, 5000, 7000, 10000, 15000, 20000, 30000, 35000, 40000}

        if if_bw not in VALID_IFBW:
            raise ValueError(
                f"Invalid IF bandwidth {if_bw} Hz. "
                f"Allowed values: {sorted(VALID_IFBW)}"
            )

        self.s.sendall(f"SENS1:BWID {if_bw}\n".encode())
        self.check_errors()

        # IF bandwidth affects sweep time → invalidate cached value
        self.sweep_time = None

    def conf_power(self, level: float = 0):
        """
        Set output power level [dBm]
        """
        self.s.sendall(f"SOUR1:POW {level}\n".encode())
        self.check_errors()

    def conf_sweep(self, points: int = 401):
        """
        Set number of sweep points, sweep time auto.
        Returns estimated sweep time [s]
        possible values: 21, 51, 101, 201, 401, 801, 1601, 3201, 6401, 12801, 16001
        """
        VALID_POINTS = { 21, 51, 101, 201, 401, 801, 1601, 3201, 6401, 12801, 16001}
        if points not in VALID_POINTS:
            raise ValueError(
                f"Invalid number of sweep points {points}. "
                f"Allowed values: {sorted(VALID_POINTS)}"
            )
        
        self.s.sendall(f"SENS1:SWE:POIN {points}\n".encode())
        self.s.sendall(b"SENS1:SWE:TIME:AUTO ON\n")
        self.check_errors()

        # Query sweep time
        return self.get_sweeptime()
    
    def get_sweeptime(self):
        """
        Query sweep time and update internal variable
        """
        self.s.sendall(b"SENS1:SWE:TIME?\n")
        sweep_time_str = recv_line(self.s)
        try:
            self.sweep_time = float(sweep_time_str)
        except ValueError:
            self.sweep_time = None
            log_message(f"{self.name}: could not read sweep time")
        return self.sweep_time
    
    # ------------------------------
    # Internal functions
    # ------------------------------

    def send_trigger(self):
        self.s.sendall(b"*CLS\n")
        self.s.sendall(b"INIT1\n") # fixed setting: channel 1
    
    def wait_for_acquisition_to_finish__OLD(self, timeout_time):
        '''
        reads VNA status and checks if device replies with value != 0. If yes, then the scan is complete.
        timeout = [0,...] timeout in [s]
        '''
        complete = False
        timeout = False
        start = time.time()
        while complete == False:
            self.s.sendall(b":STAT:OPER:DEV?\n")
            status = int(self.s.recv(100))
            # print(status)
            if status != 0: # if status != 0, the scan was finished
                complete = True
            if time.time() - start > timeout_time:
                log_message('measurement timed out')
                complete = True
                timeout = True
            time.sleep(0.05) #LabVIEW: 20 ms
        return timeout
    
    def wait_for_acquisition_to_finish(self, timeout_time):
        """Wait until sweep is finished using *OPC?"""
        old_timeout = self.s.gettimeout()
        self.s.settimeout(timeout_time)
        try:
            self.s.sendall(b"*OPC?\n")
            recv_line(self.s)
            return False  # completed successfully
        except socket.timeout:
            log_message(f"{self.name}: measurement timed out")
            return True   # timeout
        finally:
            self.s.settimeout(old_timeout)

    def get_frequency_axis(self):
        """
        Agilent PNA Series VNA cannot send out the frequency axis
        -> reconstruct it from sweep settings readback
        """
        self.s.sendall(b"SENS1:FREQ:START?\n")
        f_start = float(recv_line(self.s))
        self.s.sendall(b"SENS1:FREQ:STOP?\n")
        f_stop = float(recv_line(self.s))
        self.s.sendall(b"SENS1:SWE:POIN?\n")
        points = int(recv_line(self.s))
        return np.linspace(f_start, f_stop, points)
    
    def power_on(self):
        """
        Enable RF output
        """
        self.s.sendall(b"OUTP ON\n")
        self.check_errors()

    def power_off(self):
        """
        Disable RF output
        """
        self.s.sendall(b"OUTP OFF\n")
        self.check_errors()

    def check_errors(self, raise_on_error: bool = False):
        """
        Read and clear SCPI error queue.

        Parameters
        ----------
        raise_on_error : bool
            If True, raise RuntimeError on first negative error code
        """
        errors = []
        while True:
            self.s.sendall(b"SYST:ERR?\n")
            resp = recv_line(self.s)

            try:
                code_str, msg = resp.split(",", 1)
                code = int(code_str)
                msg = msg.strip().strip('"')
            except Exception:
                log_message(f"{self.name}: malformed SCPI error response: {resp}")
                break

            if code == 0:
                break

            errors.append((code, msg))

            level = "WARNING" if code > 0 else "ERROR"
            log_message(f"{self.name}: SCPI {level} {code}: {msg}")

            if raise_on_error and code < 0:
                raise RuntimeError(f"SCPI error {code}: {msg}")

        return errors

    def reset(self):
        """Reset device (*RST;*CLS;*OPC?)"""
        self.s.sendall(b"*RST;*CLS;*OPC?\n")
        #self.s.sendall(b"SYST:PRES;WAI;*OPC?\n") # LabVIEW version
        resp = recv_line(self.s)
        if not (resp.strip() == '+0,"No error"' or resp.strip() == '+1'):
            log_message(f"{self.name}: SCPI message: {resp}")
        self.check_errors()

# ------------------------------
# Low-level functions
# ------------------------------

def recv_exact(sock, n):
    """Receive exactly n bytes"""
    data = b""
    while len(data) < n:
        data += sock.recv(n - len(data))
    return data

def recv_line(sock):
    """Receive one SCPI line ending with LF"""
    data = b""
    while not data.endswith(b"\n"):
        data += sock.recv(1)
    return data.decode().strip()

def read_binary_block(sock, dtype):
    """Parse IEEE 488.2 binary block and return numpy array"""
    if recv_exact(sock, 1) != b"#":
        raise ValueError("No IEEE 488.2 block")
    n_digits = int(recv_exact(sock, 1))
    n_bytes = int(recv_exact(sock, n_digits))
    raw = recv_exact(sock, n_bytes)
    # wait for LF after binary block
    try:
        tail = sock.recv(1)
        if tail != b"\n":
            while tail != b"\n":
                tail = sock.recv(1)
    except:
        pass
    data = np.frombuffer(raw, dtype=dtype)
    return data

# here comes debugging and live testing
if __name__ == "__main__":
    vna = Agilent_E8358A(name="E8358A", address="192.168.147.75")
    vna.connect()
    vna.conf_measurement(measurement_type="S11")
    vna.conf_frequency(start_freq=1e9, stop_freq=2e9)
    vna.conf_bandwidth(if_bw=500)
    sweeptime = vna.conf_sweep(points=401)
    print("Sweep time:", sweeptime, "s")
    print('finshed conf_sweep')
    vna.conf_power(level=0)
    print('finshed conf_power')
    data = vna.read_data(data_mode="SDATA")
    data = vna.read_data(data_mode="SDATA")
    #print(data)

    vna.disconnect()