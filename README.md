# Фід automoto.ua → Meta Catalog (vehicle_offer)

Ти вставляєш посилання на каталоги в `catalogs.txt`, скрипт збирає оголошення і щодня оновлює
`docs/catalog_vehicle_offer.csv` та `docs/catalog_vehicle_offer.xml` у форматі твого шаблону.

## Налаштування (один раз, ~10 хв)
1. Створи репозиторій на GitHub (публічний - у фіді лише публічні дані оголошень) і залий туди всі файли цієї папки.
2. Settings → Pages → Source: "Deploy from a branch", branch `main`, папка `/docs`. Отримаєш адресу виду
   `https://<логін>.github.io/<репо>/catalog_vehicle_offer.csv` (є і `.xml`).
3. Actions → "Update Meta feed" → Run workflow - перший запуск вручну. Далі він сам іде щодня.
4. Meta Commerce Manager → Catalog → Data sources → Add items → Data feed → Scheduled feed → встав
   адресу з п.2, частота: щодня. Тип каталогу має бути "Vehicle offers".

## Як додати каталог
Відкрий `catalogs.txt` і додай рядок:
`https://automoto.ua/uk/car/Volkswagen | vw`
Частина після `|` - твоя мітка (йде в `product_tags`, `custom_label_1`), по ній у Meta зручно робити набори товарів.
Зміни зберегти (commit) - і при наступному запуску (або кнопкою Run workflow) каталог підтягнеться.

## Налаштування в build_feed.py (початок файлу)
валюта ціни, ліміт оголошень на каталог, UTM-мітки, тег зображення тощо.

## Якщо щось пішло не так
- `python build_feed.py --debug` збереже HTML перших сторінок каталогів у `debug/` - по ньому легко підправити парсер.
- Якщо оголошень раптом стало вдвічі менше, ніж учора, старий фід не перезаписується (захист від збою сайту).
- Видалені оголошення (404) автоматично зникають з фіду.
