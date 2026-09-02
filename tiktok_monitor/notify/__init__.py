"""通知の送信部品。

宛先の決め方は2系統で異なるが、ここに置くのは「送る手段」だけで、
「どこに送るか」の判断は呼び出し側が持つ:

  系統1(エラー通知): ルームIDは環境変数 CHATWORK_ERROR_ROOM_ID 固定。
                     UI・DBを一切持たない(ops/error_notifier.py)。
  系統2(進捗通知)  : ルームIDは notification_groups テーブル。UIで編集。

APIトークン(CHATWORK_API_TOKEN)と Healthchecks の UUID は、どちらの
系統でも常に環境変数から読む。DBに入れない理由は、DBが検証・バックアップ
のたびに複製されるため(認証情報を複製に同伴させない)。
"""
