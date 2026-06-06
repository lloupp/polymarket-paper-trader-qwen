import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scanner
from scanner import (
    GammaMarket,
    _parse_weather_date,
    _parse_weather_location,
    _parse_weather_metric,
    _weather_probability_from_forecast,
    detect_weather_forecast,
)
from settlement import DEFAULT_EXECUTION_POLICY, apply_execution_filters


class WeatherParsingTest(unittest.TestCase):
    def test_parse_rain_market(self):
        text = "Will it rain in New York on June 3?"

        self.assertEqual(_parse_weather_location(text), "New York")
        now = datetime(2026, 5, 28, tzinfo=timezone.utc)
        self.assertEqual(_parse_weather_date(text, now=now), "2026-06-03")
        self.assertEqual(_parse_weather_metric(text), {"metric": "rain"})

    def test_parse_temperature_market(self):
        text = "NYC high temperature above 90F on June 3"

        metric = _parse_weather_metric(text)

        self.assertEqual(_parse_weather_location(text), "NYC")
        self.assertEqual(metric["metric"], "temperature")
        self.assertEqual(metric["operator"], "above")
        self.assertEqual(metric["threshold"], 90.0)
        self.assertEqual(metric["unit"], "fahrenheit")
        self.assertEqual(metric["temp_kind"], "max")

    def test_temperature_probability_requires_clear_margin(self):
        forecast = {
            "daily": {
                "time": ["2026-06-03"],
                "temperature_2m_max": [98.0],
                "temperature_2m_min": [70.0],
                "precipitation_probability_max": [0],
                "precipitation_sum": [0.0],
            }
        }
        spec = {
            "metric": "temperature",
            "operator": "above",
            "threshold": 90.0,
            "unit": "fahrenheit",
            "temp_kind": "max",
        }

        result = _weather_probability_from_forecast(spec, forecast, "2026-06-03")

        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.7)

    def test_weather_strategy_is_shadow_by_default(self):
        accepted, rejected = apply_execution_filters(
            [
                {
                    "strategy": "weather_forecast",
                    "event_slug": "rain-nyc",
                    "direction": "yes",
                    "market_probability": 0.45,
                    "edge": 0.15,
                    "spread": 0.02,
                    "liquidity": 5000,
                    "volume_24hr": 10000,
                }
            ],
            DEFAULT_EXECUTION_POLICY,
        )

        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("shadow", rejected[0]["reason"])


class WeatherStrategyTest(unittest.IsolatedAsyncioTestCase):
    async def test_detect_weather_forecast_generates_shadow_signal(self):
        old_geocode = scanner._weather_geocode
        old_forecast = scanner._weather_forecast
        try:
            async def fake_geocode(client, location):
                return {
                    "name": "New York",
                    "admin1": "New York",
                    "country": "United States",
                    "latitude": 40.71,
                    "longitude": -74.01,
                }

            async def fake_forecast(client, geo, unit):
                return {
                    "daily": {
                        "time": ["2026-06-03"],
                        "temperature_2m_max": [80.0],
                        "temperature_2m_min": [65.0],
                        "precipitation_probability_max": [85],
                        "precipitation_sum": [7.0],
                    }
                }

            scanner._weather_geocode = fake_geocode
            scanner._weather_forecast = fake_forecast
            market = GammaMarket(
                market_id="m1",
                event_slug="will-it-rain-in-new-york-on-june-3",
                event_title="Will it rain in New York on June 3?",
                question="Will it rain in New York on June 3?",
                yes_price=0.42,
                no_price=0.58,
                spread=0.02,
                liquidity=5000,
                volume_24hr=10000,
            )

            signals = await detect_weather_forecast(
                [market],
                now=datetime(2026, 5, 28, tzinfo=timezone.utc),
            )

            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0].strategy, "weather_forecast")
            self.assertEqual(signals[0].direction, "yes")
            self.assertGreater(signals[0].edge, 0.08)
        finally:
            scanner._weather_geocode = old_geocode
            scanner._weather_forecast = old_forecast


if __name__ == "__main__":
    unittest.main()
