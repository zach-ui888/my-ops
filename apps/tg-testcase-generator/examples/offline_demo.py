"""Synthetic end-to-end example, with no network or secrets.

Run from project root: PYTHONPATH=src python3 examples/offline_demo.py
"""
from tg_testcase import Engine, Store
from tg_testcase.models import CriticalPath, Evidence, FinalRule, Requirement, TestCase
from tg_testcase.storage import PROJECT


def main():
    store = Store(PROJECT / 'data' / 'demo')
    store.recover()
    engine = Engine(store, {'demo-user'})
    task = engine.create('demo-user')

    def call(method, *args):
        nonlocal task
        task = getattr(engine, method)(task.id, 'demo-user', task.version, *args)

    call('add_source', 'demo.md', '支付成功后，订单状态为已支付。'.encode())
    call('start_review')
    call('design',
         [Requirement('R1', '支付成功更新订单状态', [task.sources[0].id])],
         [FinalRule('F1', '支付成功后的订单状态为已支付', ['R1'])],
         [CriticalPath('P1', '支付成功', ['F1'], ['订单已支付'])],
         [TestCase('Z001', '支付', '支付成功更新订单', '存在待支付订单',
                   ['完成支付并打开订单详情'], ['订单状态显示已支付'],
                   [Evidence('P1', '订单已支付', '完成支付并打开订单详情', '订单状态显示已支付')])])
    call('ready')
    print(engine.summary(task.id, 'demo-user'))
    # Simulated explicit user input; a future controller must receive this from
    # the owner, never infer it from a source document or generated text.
    call('confirm', '生成')
    call('generate')
    print(store.directory(task.id) / task.outputs[-1]['file'])
    call('cancel')  # Frees the demo user's slot; artifacts are retained.


if __name__ == '__main__':
    main()
