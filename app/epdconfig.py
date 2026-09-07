# Minimal epdconfig for non-Raspberry boards using python-periphery (Radxa, Armbian)
import os, sys, time, logging
from periphery import GPIO, SPI

logger = logging.getLogger(__name__)

# Defaults. Override with env if needed.
SPI_DEV  = os.getenv("EPD_SPI_DEV",  "/dev/spidev3.0")
GPIO_CHIP= os.getenv("EPD_GPIO_CHIP", "/dev/gpiochip0")
PIN_DC   = int(os.getenv("EPD_PIN_DC",   "25"))   # set to your wiring
PIN_RST  = int(os.getenv("EPD_PIN_RST",  "17"))
PIN_BUSY = int(os.getenv("EPD_PIN_BUSY", "24"))
PIN_PWR  = int(os.getenv("EPD_PIN_PWR",  "18"))   # optional; tie high if unused
SPI_HZ   = int(os.getenv("EPD_SPI_HZ",   "2000000"))  # 2 MHz safe
# A panel that never drops BUSY would hang the (non-vendored) upstream driver's
# busy-wait loop. Bound it in our layer: digital_read raises once the panel has
# been continuously busy this long (a full refresh takes ~2-3s).
BUSY_TIMEOUT_SEC = float(os.getenv("EPD_BUSY_TIMEOUT_SEC", "10"))
# Add a dummy CS so the driver stops failing. Kernel SPI handles CS.
PIN_CS  = int(os.getenv("EPD_PIN_CS", "-1"))
# Back-compat constants expected by waveshare drivers
RST_PIN  = PIN_RST
DC_PIN   = PIN_DC
BUSY_PIN = PIN_BUSY
PWR_PIN  = PIN_PWR

_spi = None
_gpio_dc = None
_gpio_rst = None
_gpio_busy = None
_gpio_pwr = None
_busy_since = None

def _open_gpio(line, direction):
    # direction: "in" or "out"
    return GPIO(GPIO_CHIP, line, direction)

def digital_write(pin, value):
    if pin == PIN_DC and _gpio_dc:
        _gpio_dc.write(bool(value))
    elif pin == PIN_RST and _gpio_rst:
        _gpio_rst.write(bool(value))
    elif pin == PIN_PWR and _gpio_pwr:
        _gpio_pwr.write(bool(value))
    elif pin == PIN_CS:
        # no-op; /dev/spidev manages CS
        return

def digital_read(pin):
    # BUSY is active-high on the 2.13" V4, and epd2in13_V4.ReadBusy() loops
    # `while digital_read(busy_pin) == 1` documenting "0: idle, 1: busy", so the
    # level passes straight through. The watchdog therefore counts time spent
    # HIGH: LOW is the panel's resting state between refreshes and a static
    # screen can sit there for minutes.
    global _busy_since
    if pin == PIN_BUSY and _gpio_busy:
        if not _gpio_busy.read():
            _busy_since = None
            return 0
        # Still busy: raise rather than let the driver's busy-wait hang forever.
        now = time.monotonic()
        if _busy_since is None:
            _busy_since = now
        elif now - _busy_since > BUSY_TIMEOUT_SEC:
            _busy_since = None
            raise RuntimeError(f"display stuck busy for >{BUSY_TIMEOUT_SEC:g}s")
        return 1
    return 0

def delay_ms(ms):
    time.sleep(ms / 1000.0)

def _as_bytes_list(data):
    # Accept list/tuple/bytes/bytearray
    if isinstance(data, (bytes, bytearray)):
        return list(data)
    return [int(x) & 0xFF for x in data]

def spi_writebyte(data):
    # Write without CS toggle between bytes.
    tx = _as_bytes_list(data)
    _spi.transfer(tx)

def spi_writebyte2(data):
    # Same semantics as writebytes2 in spidev
    tx = _as_bytes_list(data)
    _spi.transfer(tx)

def module_init(cleanup=False):
    # Re-entrant: the driver calls this from both init() and init_fast(), and
    # Panel uses both (full mode, then partial mode). A kernel gpiochip refuses
    # a second request for a line this process already holds, so release what we
    # hold before reopening. Both callers hardware-reset the panel straight
    # after, so re-driving the lines here is safe.
    global _spi, _gpio_dc, _gpio_rst, _gpio_busy, _gpio_pwr
    module_exit()
    try:
        # Open SPI
        _spi = SPI(SPI_DEV, 0, SPI_HZ)  # mode 0
        # Open GPIOs
        _gpio_dc   = _open_gpio(PIN_DC,   "out")
        _gpio_rst  = _open_gpio(PIN_RST,  "out")
        _gpio_busy = _open_gpio(PIN_BUSY, "in")
        try:
            _gpio_pwr = _open_gpio(PIN_PWR, "out")
            _gpio_pwr.write(True)
        except Exception:
            _gpio_pwr = None  # optional pin
    except Exception:
        # Don't leak whichever handles did open, and re-raise unchanged: Panel's
        # _safe_init_call recovers from a GPIOError with errno 16, which a
        # wrapper exception would hide.
        logger.warning("display init failed", exc_info=True)
        module_exit()
        raise
    return 0

def module_exit(cleanup=False):
    # Never raises: it is the cleanup path for a failed init as well as the
    # teardown for a healthy one.
    global _spi, _gpio_dc, _gpio_rst, _gpio_busy, _gpio_pwr, _busy_since
    try:
        if _spi: _spi.close()
    except Exception:
        pass
    for g in (_gpio_dc, _gpio_rst, _gpio_busy, _gpio_pwr):
        try:
            if g: g.close()
        except Exception:
            pass
    _spi = _gpio_dc = _gpio_rst = _gpio_busy = _gpio_pwr = None
    _busy_since = None

# Backward-compat API expected by waveshare drivers
# The driver dynamically imports all non-private names in this module,
# so we just expose the functions above.
# ---- Back-compat constants expected by drivers ----
try:
    PIN_CS
except NameError:
    PIN_CS = int(os.getenv("EPD_PIN_CS", "-1"))  # dummy; spidev handles CS

RST_PIN  = PIN_RST
DC_PIN   = PIN_DC
BUSY_PIN = PIN_BUSY
PWR_PIN  = PIN_PWR
CS_PIN   = PIN_CS
# ---------------------------------------------------
