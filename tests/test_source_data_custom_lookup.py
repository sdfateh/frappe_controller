"""Custom catalog names remain usable without becoming URL path segments."""

import json
import unittest
from unittest.mock import patch

from frappe_controller import source_data as client


class CustomLookupTests(unittest.TestCase):
    def request(self, brand="Example/Division", model=None):
        filters = {"brand": brand}
        if model is not None:
            filters["model"] = model
        return client.prepare_request("years" if model is not None else "models", filters, 0, 50)

    def test_saved_names_are_passed_only_as_exact_query_filters(self):
        for value in ["Example/Division", "A\\B", ".", "..", "../colors", "%2f", "ع/ب"]:
            with self.subTest(value=value):
                saved = client.validate_fields("vehicles", {"brand": value, "model": value})
                for request in [self.request(saved["brand"]), self.request("Regular", saved["model"])]:
                    self.assertEqual("/tires/vehicle/get/list", request["path"])
                    self.assertIn(value, json.loads(request["params"]["filters"]).values())

    def test_paginated_results_are_distinct_and_nulls_are_omitted(self):
        request = self.request()
        with patch.object(client, "_get_json", side_effect=[
            ([{"model": "First"}] * 100, 103),
            ([{"model": "Second"}, {"model": None}, {"model": ""}], 103),
        ]) as get:
            self.assertEqual([{"model": "First"}, {"model": "Second"}], client.fetch_records(request))
        self.assertEqual([0, 100], [call.args[1]["start"] for call in get.call_args_list])
        self.assertEqual(2, client.result_page(request, [{"model": "First"}, {"model": "Second"}], "now")["total"])

    def test_empty_and_year_results(self):
        with patch.object(client, "_get_json", return_value=([], 0)):
            self.assertEqual([], client.fetch_records(self.request()))
        with patch.object(client, "_get_json", return_value=([{"year": 2020}, {"year": None}], None)):
            self.assertEqual([{"year": 2020}], client.fetch_records(self.request(model="A/B")))

    def test_invalid_incomplete_or_oversized_results_fail_without_partial_suggestions(self):
        for response in [({}, 0), ([{}], 1), ([{"model": True}], 1),
                         ([], 1), ([{"model": "A"}], 0), ([], 10001),
                         ([{"model": "A"}] * 101, None)]:
            with self.subTest(response=response), patch.object(client, "_get_json", return_value=response):
                with self.assertRaises(client.SourceDataError):
                    client.fetch_records(self.request())
        with patch.object(client, "MAX_RECORDS", 100), patch.object(client, "_get_json", return_value=([{"model": "A"}] * 100, None)):
            with self.assertRaisesRegex(client.SourceDataError, "browsing limit"):
                client.fetch_records(self.request())
        with patch.object(client, "MAX_RESPONSE_BYTES", 10), patch.object(client, "_get_json", return_value=([{"model": "A"}], 1)):
            with self.assertRaisesRegex(client.SourceDataError, "browsing limit"):
                client.fetch_records(self.request())

    def test_timeout_and_api_failure_propagate_without_partial_suggestions(self):
        with patch.object(client.time, "monotonic", side_effect=[0, 0, 21]), patch.object(client, "_get_json", return_value=([], 0)):
            with self.assertRaisesRegex(client.SourceDataError, "timed out"):
                client.fetch_records(self.request())
        with patch.object(client, "_get_json", side_effect=client.SourceDataError("Unavailable")):
            with self.assertRaisesRegex(client.SourceDataError, "Unavailable"):
                client.fetch_records(self.request())

    def test_structure_and_engine_query_filters_accept_custom_names(self):
        filters = {"brand": "A/B", "model": "..", "year": "2020"}
        request = client.prepare_request("structures", filters, 0, 50)
        self.assertEqual(filters, request["params"])
        filters["structure_type"] = "A/B"
        request = client.prepare_request("engines", filters, 0, 50)
        self.assertEqual("A/B", request["params"]["structureType"])


if __name__ == "__main__":
    unittest.main()
