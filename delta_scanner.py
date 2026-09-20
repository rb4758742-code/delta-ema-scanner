import requests
import time
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "https://api.india.delta.exchange"

# =========================================================
# TELEGRAM
# =========================================================

config = {}

with open("telegram_config.txt", "r") as f:
    exec(f.read(), config)

TELEGRAM_TOKEN = config["TOKEN"]
CHAT_ID = config["CHAT_ID"]


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.get(
            url,
            params={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=10
        )

    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# DELTA PRODUCTS
# =========================================================

def get_products():

    try:
        r = requests.get(
            f"{BASE_URL}/v2/products",
            timeout=10
        )

        data = r.json()["result"]

        symbols = []

        for p in data:

            if (
                p.get("contract_type") == "perpetual_futures"
                and p.get("state") == "live"
            ):
                symbols.append(p["symbol"])

        return symbols

    except Exception as e:

        print("Product error:", e)

        return []


# =========================================================
# CANDLES
# =========================================================

def get_candles(symbol, resolution):

    try:

        seconds = 15 * 60 if resolution == "15m" else 60 * 60

        r = requests.get(
            f"{BASE_URL}/v2/history/candles",
            params={
                "symbol": symbol,
                "resolution": resolution,
                "start": int(time.time()) - seconds * 70,
                "end": int(time.time())
            },
            timeout=10
        )

        data = r.json()["result"]

        if len(data) < 25:
            return []

        return sorted(
            data,
            key=lambda x: x["time"]
        )

    except Exception:

        return []


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    multiplier = 2 / (period + 1)

    result = [values[0]]

    for price in values[1:]:

        result.append(
            (price - result[-1]) * multiplier
            + result[-1]
        )

    return result


# =========================================================
# STATE
#
# Each coin + timeframe has its own state.
# =========================================================

states = {}


def get_state(symbol, timeframe):

    key = (symbol, timeframe)

    if key not in states:

        states[key] = {
            "stage": "WAIT_CROSSOVER",
            "direction": None,
            "cross_time": None,
            "retest_range": None,
            "retest_time": None,
            "last_closed_time": None
        }

    return states[key]


# =========================================================
# SCANNER
# =========================================================

def scan_setup(symbol, timeframe):

    candles = get_candles(symbol, timeframe)

    if len(candles) < 25:
        return

    # Last candle is considered currently forming.
    # We work with CLOSED candles only.
    closed = candles[:-1]

    if len(closed) < 20:
        return

    closes = [
        float(c["close"])
        for c in closed
    ]

    ema9 = calculate_ema(closes, 9)
    ema15 = calculate_ema(closes, 15)

    state = get_state(symbol, timeframe)

    i = len(closed) - 1

    current_closed_time = closed[i]["time"]

    # -----------------------------------------------------
    # FIRST TIME SCANNER SEES THIS COIN + TIMEFRAME
    #
    # Ignore all old/historical setups.
    # -----------------------------------------------------

    if state["last_closed_time"] is None:

        state["last_closed_time"] = current_closed_time

        return

    # Same candle already processed
    if current_closed_time == state["last_closed_time"]:

        return

    state["last_closed_time"] = current_closed_time

    candle = closed[i]

    high = float(candle["high"])
    low = float(candle["low"])
    open_price = float(candle["open"])
    close_price = float(candle["close"])

    e9 = ema9[i]
    e15 = ema15[i]

    # =====================================================
    # NEW CROSSOVER
    # =====================================================

    cross_long = (
        ema9[i] > ema15[i]
        and ema9[i - 1] <= ema15[i - 1]
    )

    cross_short = (
        ema9[i] < ema15[i]
        and ema9[i - 1] >= ema15[i - 1]
    )

    if cross_long:

        state["stage"] = "WAIT_AWAY"
        state["direction"] = "LONG"
        state["cross_time"] = candle["time"]
        state["retest_range"] = None
        state["retest_time"] = None

        print(
            f"[{timeframe}] {symbol} 🟢 NEW LONG CROSSOVER"
        )

        return

    if cross_short:

        state["stage"] = "WAIT_AWAY"
        state["direction"] = "SHORT"
        state["cross_time"] = candle["time"]
        state["retest_range"] = None
        state["retest_time"] = None

        print(
            f"[{timeframe}] {symbol} 🔴 NEW SHORT CROSSOVER"
        )

        return

    # =====================================================
    # WAITING FOR CROSSOVER
    # =====================================================

    if state["stage"] == "WAIT_CROSSOVER":

        return

    # =====================================================
    # LOCKED
    #
    # Failed setup or completed setup.
    # Only NEW crossover can unlock it.
    # =====================================================

    if state["stage"] == "LOCKED":

        return

    # =====================================================
    # WAIT FOR PRICE TO MOVE AWAY
    # =====================================================

    if state["stage"] == "WAIT_AWAY":

        if state["direction"] == "LONG":

            if low > e9 and low > e15:

                state["stage"] = "WAIT_RETEST"

                print(
                    f"[{timeframe}] {symbol} ↗ Price moved ABOVE EMA zone"
                )

        elif state["direction"] == "SHORT":

            if high < e9 and high < e15:

                state["stage"] = "WAIT_RETEST"

                print(
                    f"[{timeframe}] {symbol} ↘ Price moved BELOW EMA zone"
                )

        return

    # =====================================================
    # FIRST RETEST
    #
    # Candle must touch BOTH EMA 9 and EMA 15.
    # =====================================================

    if state["stage"] == "WAIT_RETEST":

        touches_ema9 = (
            low <= e9 <= high
        )

        touches_ema15 = (
            low <= e15 <= high
        )

        if touches_ema9 and touches_ema15:

            retest_range = high - low

            if retest_range <= 0:
                return

            state["retest_range"] = retest_range
            state["retest_time"] = candle["time"]

            state["stage"] = "WAIT_CONFIRM"

            print(
                f"[{timeframe}] {symbol} 🔄 FIRST RETEST FOUND"
            )

        return

    # =====================================================
    # NEXT CANDLE CONFIRMATION
    #
    # This is the ONLY candle allowed to confirm.
    # =====================================================

    if state["stage"] == "WAIT_CONFIRM":

        # Current candle is immediately after retest
        if candle["time"] <= state["retest_time"]:

            return

        # -----------------------------------------------
        # NEXT CANDLE MUST TOUCH BOTH EMAs
        # -----------------------------------------------

        touches_ema9 = (
            low <= e9 <= high
        )

        touches_ema15 = (
            low <= e15 <= high
        )

        touches_both = (
            touches_ema9 and touches_ema15
        )

        # -----------------------------------------------
        # NEXT CANDLE DIRECTION
        # -----------------------------------------------

        if state["direction"] == "LONG":

            correct_direction = (
                close_price > open_price
            )

        else:

            correct_direction = (
                close_price < open_price
            )

        # -----------------------------------------------
        # NEXT CANDLE RANGE >= 50%
        # OF RETEST CANDLE
        # -----------------------------------------------

        current_range = high - low
        retest_range = state["retest_range"]

        half_condition = (
            current_range >= retest_range * 0.50
        )

        # -----------------------------------------------
        # ALL CONDITIONS
        # -----------------------------------------------

        if (
            touches_both
            and correct_direction
            and half_condition
        ):

            direction = state["direction"]

            message = (
                "🚨 EMA CONFIRMED SIGNAL\n\n"
                f"Coin: {symbol}\n"
                f"Timeframe: {timeframe}\n"
                f"Signal: {direction}\n"
                f"Price: {close_price}\n\n"
                "✅ Fresh crossover\n"
                "✅ Price moved away\n"
                "✅ First retest\n"
                "✅ Retest touched EMA 9 + EMA 15\n"
                "✅ Next candle confirmed direction\n"
                "✅ Next candle touched both EMAs\n"
                "✅ Next candle range >= 50%\n\n"
                "⚡ SETUP CONFIRMED"
            )

            print("\n" + "=" * 45)
            print(message)
            print("=" * 45)

            send_telegram(message)

            # Lock until a NEW crossover
            state["stage"] = "LOCKED"

        else:

            print(
                f"[{timeframe}] {symbol} ❌ Confirmation FAILED"
            )

            # No second retest allowed
            state["stage"] = "LOCKED"

        return


# =========================================================
# MAIN
# =========================================================

print("================================================")
print("       DELTA EMA MULTI-TIMEFRAME SCANNER")
print("================================================")
print("Timeframes      : 15M + 1H")
print("EMA             : 9 / 15")
print("Both EMA Touch  : REQUIRED")
print("First Retest    : REQUIRED")
print("Next Candle     : REQUIRED")
print("50% Range       : REQUIRED")
print("Historical      : IGNORED")
print("Telegram        : ON")
print("================================================")

send_telegram(
    "🟢 Delta EMA Scanner Started\n\n"
    "Timeframes: 15M + 1H\n"
    "EMA 9/15\n"
    "First retest + confirmation ON"
)


while True:

    try:

        symbols = get_products()

        print(
            f"\nScanning {len(symbols)} live perpetual coins..."
        )

        # ---------------------------------------------
        # 15M + 1H
        # ---------------------------------------------

        jobs = []

        for symbol in symbols:

            jobs.append((symbol, "15m"))
            jobs.append((symbol, "1h"))

        def run_job(job):

            symbol, timeframe = job

            scan_setup(
                symbol,
                timeframe
            )

        with ThreadPoolExecutor(
            max_workers=10
        ) as executor:

            list(
                executor.map(
                    run_job,
                    jobs
                )
            )

        print(
            "Scan complete. Waiting 30 seconds..."
        )

        time.sleep(30)

    except KeyboardInterrupt:

        print("\nScanner stopped.")

        break

    except Exception as e:

        print(
            "Main error:",
            e
        )

        time.sleep(30)