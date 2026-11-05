"""Default-product contract, membership, and clone identity tests; no live mutations."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import mayzing, product_defaults


class ProductDefaultTests(unittest.TestCase):
    def group(self):
        return {'id': 'group', 'heroProductId': 'shirt', 'productIds': ['shirt', 'hoodie', 'mug'],
                'products': [{'id': 'shirt', 'baseProductId': 'base-shirt'},
                             {'id': 'hoodie', 'baseProductId': 'base-hoodie'},
                             {'id': 'mug', 'baseProductId': 'base-mug'}]}

    def test_order_mutation_preserves_all_other_products(self):
        client = mayzing.Mayzing()
        expected = self.group() | {'heroProductId': 'hoodie', 'productIds': ['hoodie', 'shirt', 'mug']}
        client.gql = Mock(return_value={'updateStoreGroupProductOrder': expected})
        self.assertEqual(client.set_first_product('STORE', self.group(), 'hoodie'), expected)
        self.assertEqual(client.gql.call_args.args[1], {'input': {
            'id': 'group', 'storeSlug': 'STORE', 'productIds': ['hoodie', 'shirt', 'mug']}})
        self.assertEqual(client.gql.call_args.kwargs['op'], 'UpdateStoreGroupProductOrder')
        self.assertIn('UpdateStoreGroupProductOrder', mayzing.MUTATIONS)

    def test_other_campaign_product_is_rejected_before_mutation(self):
        client = mayzing.Mayzing()
        client.gql = Mock()
        with self.assertRaises(mayzing.MayzingError):
            client.set_first_product('STORE', self.group(), 'other-campaign-product')
        client.gql.assert_not_called()

    def test_existing_default_does_not_send_mutation(self):
        client = mayzing.Mayzing()
        client.gql = Mock()
        client.set_first_product('STORE', self.group(), 'shirt')
        client.gql.assert_not_called()

    def test_wrong_confirmed_default_is_an_error(self):
        client = mayzing.Mayzing()
        client.gql = Mock(return_value={'updateStoreGroupProductOrder': self.group()})
        with self.assertRaises(mayzing.MayzingError):
            client.set_first_product('STORE', self.group(), 'hoodie')
        client.gql.assert_called_once()

    def test_clone_is_resolved_by_base_product_not_origin_id(self):
        group = self.group()
        self.assertEqual(product_defaults.resolve(group, 'base-hoodie'), 'hoodie')
        group['products'].append({'id': 'second-hoodie', 'baseProductId': 'base-hoodie'})
        group['productIds'].append('second-hoodie')
        with self.assertRaises(mayzing.MayzingError):
            product_defaults.resolve(group, 'base-hoodie')
        with self.assertRaises(mayzing.MayzingError):
            product_defaults.resolve(group, 'not-in-this-campaign')

    def test_options_use_catalog_names_in_campaign_order(self):
        result = product_defaults.options(self.group(), {'base-shirt': 'T-shirt', 'base-hoodie': 'Hoodie', 'base-mug': 'Mug'})
        self.assertEqual([p['label'] for p in result], ['T-shirt', 'Hoodie', 'Mug'])
        self.assertEqual([p['id'] for p in result], ['shirt', 'hoodie', 'mug'])
        self.assertTrue(result[0]['current'])

    def test_observed_base_name_works_for_new_cloned_product_ids(self):
        group = self.group()
        group['products'][1]['id'] = 'new-clone-hoodie'
        group['productIds'][1] = 'new-clone-hoodie'
        result = product_defaults.options(group, {}, {'base-hoodie': 'Hoodie'})
        self.assertEqual(result[1]['label'], 'Hoodie')

    def test_old_extension_cannot_send_new_operations(self):
        from unittest.mock import patch
        from lib.browser_bridge import BrowserBridge
        broker = BrowserBridge()
        with patch.object(mayzing, 'BRIDGE', broker):
            with self.assertRaisesRegex(mayzing.MayzingError, '1.1.0'):
                mayzing._request('StoreGroupWithProducts', {'variables': {}})
        self.assertFalse(broker.commands)


if __name__ == '__main__':
    unittest.main(verbosity=2)
