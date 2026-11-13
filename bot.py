import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from config import TOKEN, GROUPS, PROFANITY_FILE

# Слабые записи могут задеть обычные сообщения. False — не проверять их.
USE_WEAK = True

# Файл со списком спама и 18+ (лежит в корне репозитория рядом с bot.py)
SPAM_FILE = "spam_words.txt"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("profanity-bot")


# ---------- HTTP-сервер для Render и UptimeRobot ----------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_http_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), PingHandler).serve_forever()


# ---------- Встроенные списки ----------

# Корни мата: слово ловится, если начинается с корня
MAT_ROOTS = set("""
хуй хуе хуя хую хуйн хуен нахуй нахуя похуй похуя захуя расхуя выхуя нихуя охуе охуи ахуе ахуи ахуен
пизд спизд запизд припизд распизд опизд упизд выпизд пезд
ебал ебат ебан ебу ебл ебн ебо ебе еби ебы уеб заеб наеб выеб поеб проеб объеб отъеб подъеб разъеб съеб въеб доеб
блят бляд мудак мудил мудо пидор пидар пидр педик
сука суки сукин сучк сучар шлюх шлюш шалав шмар залуп гондон гандон
дроч отдроч подроч надроч письк писюн писун сиськ титьк жоп
херн херов херач херак нахер нахрен нихер нихрен похер похрен
срат срал срун срак говн дерьм ссат ссан ссык
""".split())

# Точные слова: совпадают только целиком
MAT_EXACT = {"еб", "хер", "бля", "пенис", "манда", "муди", "мудя", "куни", "ххх"}

# Слова из файла, которые нельзя считать корнем (иначе ловятся обычные слова)
EXACT_ONLY = {"анал", "вирт", "досуг", "дропы", "закладки", "меф", "соль",
              "шишки", "ню", "фанс", "сливы", "путана"}

# Слабые записи: могут задеть обычные сообщения. Выключаются через USE_WEAK
WEAK_ENTRIES = {
    "досуг", "вирт", "соль", "шишки", "закладки", "сливы",
    "сочная", "мокрая", "страстная", "развратная", "пошлая", "пошлые",
    "озабоченная", "озабоченный", "раздетая", "раздетые", "горячая штучка",
    "в чулках", "в одном белье", "в нижнем белье", "без лифчика", "без трусов",
    "показать грудь", "горячие девушки", "девочки ждут", "девушки онлайн",
    "девушки рядом", "одинокая девушка", "одинокая ищет", "ищу мужчину",
    "ищу парня для встреч", "встречусь сегодня", "приеду к тебе", "выезд на дом",
    "приглашу в гости", "хочу тебя", "скинь фото", "пришли фото", "отправлю фото",
    "хочешь фото", "фото в лс", "видео в лс", "знакомства рядом", "знакомства на ночь",
    "знакомства для измены", "анонимные встречи", "встречи без обязательств",
    "для тайных встреч", "проверка безопасности", "узнать местоположение",
    "платный контент", "запретный контент", "откровенные фото", "откровенные видео",
    "горячая переписка", "пошлая переписка", "пообщаемся откровенно",
    "поговорим откровенно", "любовница", "ищу любовника", "ищу любовницу",
    "для замужних", "для женатых", "доминант",
}

# Безобидные слова, которые начинаются с корня мата или спама
SAFE = ("ебург", "трахе", "трахом", "педикюр", "педикул", "бляш", "секстан",
        "сексолог", "сексист", "сексизм", "сексопат")

# Латинские буквы и цифры, похожие на русские: "xуй" → "хуй"
RU_MAP = str.maketrans({
    "a": "а", "b": "б", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
    "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
    "ё": "е", "0": "о", "1": "и", "3": "з", "6": "б",
})

# 18+ в разных записях
AGE_RE = re.compile(r"\+\s*18|18\s*\+|18\s*plus|18\s*плюс|восемнадцать\s*плюс")

# Буквы заменены знаками: "х*й", "п*зд", "е**ать", "м*д*а*к"
MASKED_RE = re.compile(
    r"х[^\w\s]+й|п[^\w\s]+з[^\w\s]*д|е[^\w\s]+(?:б|ать|ат)|м[^\w\s]+д[^\w\s]*а[^\w\s]*к"
)

EXACT, ROOTS, PHRASES = set(), set(), set()


# ---------- Загрузка списков ----------

def variants(s):
    """Слово в двух видах: как написано и с заменой похожих букв."""
    return {s, s.translate(RU_MAP)}


def norm(s):
    """Оставляет только буквы и цифры, слова через один пробел."""
    return " ".join(re.findall(r"[a-zа-яё0-9]+", s))


def add_entry(entry, weak=False):
    """Кладёт запись из списка в нужную группу: точные слова, корни или фразы."""
    entry = re.sub(r"\[[^\]]*\]\*", "", entry.lower()).strip(" *")  # хвосты regex из файла
    age = bool(AGE_RE.search(entry))
    if age:
        entry = AGE_RE.sub(" ", entry)
        weak = True
    if weak and not USE_WEAK:
        return
    parts = re.findall(r"[a-zа-яё0-9]+", entry)
    if len(parts) >= 3 and all(len(p) == 1 for p in parts):
        parts = ["".join(parts)]  # с е к с → секс
    if not parts:
        return
    if len(parts) == 1:
        word = parts[0]
        if age:
            return  # "девушки 18 плюс" → "девушки": слишком общее слово
        if len(word) <= 2 or word in EXACT_ONLY:
            EXACT.update(variants(word))
        else:
            ROOTS.update(variants(word))
    else:
        PHRASES.update(variants(" ".join(parts)))


def load_lists():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SPAM_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                # записи в одной строке разделены двумя и более пробелами
                for entry in re.split(r"\s{2,}", line.strip()):
                    entry = entry.strip()
                    if entry:
                        add_entry(entry, weak=entry.lower() in WEAK_ENTRIES)
    except FileNotFoundError:
        log.warning("Файл %s не найден, список спама не загружен", SPAM_FILE)

    for word in MAT_ROOTS:
        ROOTS.update(variants(word))
    EXACT.update(MAT_EXACT)

    try:
        with open(PROFANITY_FILE, encoding="utf-8") as f:
            for item in json.load(f):
                add_entry(str(item))
    except FileNotFoundError:
        pass


# ---------- Проверка текста ----------

def merge_letters(parts):
    """Склеивает разбитые буквы: ['х', 'у', 'й'] → ['хуй']."""
    out, buf = [], ""
    for p in parts:
        if not p:
            continue
        if len(p) == 1:
            buf += p
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(p)
    if buf:
        out.append(buf)
    return out


def forms(low):
    """Слова текста: как написаны и с заменой похожих букв."""
    parts = [re.sub(r"[^a-zа-яё0-9]", "", w) for w in low.split()]
    return merge_letters(parts) + merge_letters([p.translate(RU_MAP) for p in parts])


def word_hit(form):
    if form in EXACT:
        return True
    return not form.startswith(SAFE) and form.startswith(tuple(ROOTS))


def is_profane(text):
    low = text.lower()
    ru = low.translate(RU_MAP)
    if USE_WEAK and AGE_RE.search(low):
        return True
    if MASKED_RE.search(ru):
        return True
    if any(word_hit(w) for w in forms(low)):
        return True
    raw_text = f" {norm(low)} "
    ru_text = f" {norm(ru)} "
    return any(f" {p} " in raw_text or f" {p} " in ru_text for p in PHRASES)


# ---------- Обработчик сообщений ----------

async def profanity_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.chat.type not in ("group", "supergroup"):
        return
    if GROUPS and message.chat.id not in GROUPS:
        return

    text = message.text or message.caption or ""
    if not text or not is_profane(text):
        return

    try:
        await context.bot.delete_message(
            chat_id=message.chat_id, message_id=message.message_id
        )
        user = message.from_user.id if message.from_user else "неизвестно"
        log.info("Удалено сообщение от %s", user)
    except Exception as e:
        log.warning("Не удалось удалить сообщение: %s", e)


def main():
    load_lists()
    log.info("Загружено: точных слов %d, корней %d, фраз %d",
             len(EXACT), len(ROOTS), len(PHRASES))
    threading.Thread(target=run_http_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, profanity_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
