import sys
import unittest
from pathlib import Path

_HOME = Path.home()
for _libs in (_HOME / "tumbil" / "infrastructure" / "libs", _HOME / "infrastructure" / "libs"):
    if _libs.is_dir():
        sys.path.insert(0, str(_libs))
        break

from ga4_attribution import classify_ga4_rows, parse_ga4_report, related_ga4_rows


class Ga4AttributionTests(unittest.TestCase):
    def test_parse_purchase_rows_with_custom_user_id(self):
        payload = {
            "dimensionHeaders": [
                {"name": "date"},
                {"name": "eventName"},
                {"name": "customUser:tumbil_id"},
                {"name": "sessionSourcePlatform"},
                {"name": "sessionGoogleAdsCampaignName"},
                {"name": "sessionSource"},
                {"name": "sessionMedium"},
                {"name": "sessionCampaignName"},
                {"name": "sessionDefaultChannelGroup"},
            ],
            "metricHeaders": [
                {"name": "eventCount"},
                {"name": "totalUsers"},
                {"name": "totalRevenue"},
            ],
            "rows": [{
                "dimensionValues": [
                    {"value": "20260515"},
                    {"value": "purchase"},
                    {"value": "3991"},
                    {"value": "Google Ads"},
                    {"value": "City Target (GTA) Click"},
                    {"value": "google"},
                    {"value": "cpc"},
                    {"value": "City Target (GTA) Click"},
                    {"value": "Paid Search"},
                ],
                "metricValues": [
                    {"value": "1"},
                    {"value": "1"},
                    {"value": "50.94"},
                ],
            }],
        }

        rows = parse_ga4_report(payload)

        self.assertEqual(rows[0]["tumbil_id"], "3991")
        self.assertEqual(rows[0]["session_source_platform"], "Google Ads")
        self.assertEqual(rows[0]["session_google_ads_campaign_name"], "City Target (GTA) Click")

    def test_google_ads_data_api_purchase_wins_over_direct_bigquery_row(self):
        rows = [
            {
                "source_system": "bigquery",
                "event_name": "first_open",
                "tumbil_id": "3991",
                "traffic_source": "(direct)",
                "traffic_medium": "(none)",
            },
            {
                "source_system": "ga4_data_api",
                "event_name": "purchase",
                "tumbil_id": "3991",
                "session_source_platform": "Google Ads",
                "session_google_ads_campaign_name": "City Target (GTA) Click",
                "session_source": "google",
                "session_medium": "cpc",
                "session_default_channel_group": "Paid Search",
            },
        ]

        attribution = classify_ga4_rows(rows)

        self.assertEqual(attribution["bucket"], "Google Ads")
        self.assertEqual(attribution["confidence"], "high")
        self.assertEqual(attribution["match_quality"], "ga4_purchase_tumbil_id")
        self.assertEqual(attribution["campaign"], "City Target (GTA) Click")

    def test_related_rows_include_exact_order_and_user_attribution(self):
        rows = [
            {
                "source_system": "ga4_bigquery",
                "event_name": "purchase",
                "order_id": "5542",
                "tumbil_id": "4929",
                "traffic_source": "(direct)",
                "traffic_medium": "(none)",
            },
            {
                "source_system": "ga4_data_api",
                "event_name": "purchase",
                "tumbil_id": "4929",
                "session_source_platform": "Google Ads",
                "session_google_ads_campaign_name": "City Target (GTA) Click",
                "session_source": "google",
                "session_medium": "cpc",
                "session_default_channel_group": "Paid Search",
            },
        ]

        related = related_ga4_rows(rows, "5542", "4929")
        attribution = classify_ga4_rows(related)

        self.assertEqual(len(related), 2)
        self.assertEqual(attribution["bucket"], "Google Ads")
        self.assertEqual(attribution["confidence"], "high")

    def test_unassigned_source_does_not_match_instagram_substring(self):
        attribution = classify_ga4_rows([{
            "source_system": "ga4_data_api",
            "event_name": "purchase",
            "tumbil_id": "4943",
            "session_source_platform": "(not set)",
            "session_source": "(not set)",
            "session_medium": "(not set)",
            "session_campaign": "",
            "session_default_channel_group": "Unassigned",
        }])

        self.assertEqual(attribution["bucket"], "Direct / Unknown")
        self.assertEqual(attribution["match_quality"], "ga4_direct_or_empty")

    def test_missing_attribution_stays_unknown(self):
        attribution = classify_ga4_rows([])

        self.assertEqual(attribution["bucket"], "Direct / Unknown")
        self.assertEqual(attribution["confidence"], "low")
        self.assertEqual(attribution["match_quality"], "unmatched")


if __name__ == "__main__":
    unittest.main()
