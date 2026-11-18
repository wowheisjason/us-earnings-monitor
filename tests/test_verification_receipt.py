from pathlib import Path
import runpy
import pytest


@pytest.mark.parametrize('receipt,valid', [
    ({'status': 'accepted', 'message_id': 42}, True),
    ({'status': 'sending', 'message_id': 42}, False),
    ({'status': 'accepted', 'message_id': True}, False),
    ({'status': 'accepted', 'message_id': 0}, False),
    ({'status': 'accepted'}, False),
])
def test_only_actual_accepted_message_receipt_is_success(receipt, valid):
    helper = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/verify_qa_fallback.py'))
    assert helper['valid_receipt'](receipt) is valid
