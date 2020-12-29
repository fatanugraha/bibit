#!/usr/bin/env python3
import os, requests, json, time


class StoreNotInitializedError(Exception):
    pass


class JSONFileStorage:
    def __init__(self, filename):
        self.filename = filename

    def load(self):
        try:
            with open(self.filename, "r") as fd:
                return json.load(fd)
        except FileNotFoundError:
            raise StoreNotInitializedError

    def dump(self, content):
        with open(self.filename, "w") as fd:
            json.dump(content, fd)


class SecretStore:
    def __init__(self, storage):
        self.storage = storage

    def init(self):
        content = self.storage.load()

        self.access_token = content['access_token']
        self.refresh_token = content['refresh_token']
        self.telegram_token = content['telegram_token']
        self.telegram_chat_id = content['telegram_chat_id']

    def save(self):
        content = {
            "access_token": self.access_token, 
            "refresh_token": self.refresh_token, 
            "telegram_token": self.telegram_token, 
            "telegram_chat_id": self.telegram_chat_id
        }

        self.storage.dump(content)

   
class RollingJSONFileRepository:
    def __init__(self, directory, file_prefix):
        self.directory = directory
        self.file_prefix = file_prefix

    def _get_filename(self, idx):
        return os.path.join(self.directory, f"{self.file_prefix}.{idx}.json")

    def init(self):
        try:
            os.mkdir(self.directory)
        except OSError:
            pass

        self.files = os.listdir(self.directory)
        
        # get the latest index
        self._last_idx = 1
        for f in self.files:
            _, idx, _ = f.split('.')

            idx = int(idx)
            if idx > self._last_idx:
                self._last_idx = idx

    
    def get_latest_filename(self):
        return self._get_filename(self._last_idx)
    
    def new_file(self):
        self._last_idx += 1
        return self._get_filename(self._last_idx)


class PortofolioHistoryStore:
    def __init__(self, storage):
        self.storage = storage

    def init(self):
        try:
            self.history = self.storage.load()
        except StoreNotInitializedError:
            self.history = []

    def save(self):
        self.storage.dump(self.history)

    def add(self, snapshot):
        self.history.append({"timestamp": int(time.time()), "portofolios": snapshot})
        self.save()

    def get_last(self):
        try:
            last = self.history[-1]
            return last['portofolios']
        except IndexError:
            return []

    
class RollingPortofolioHistoryStore:
    portofolio_history_storage_klass = JSONFileStorage
    portofolio_history_store_klass = PortofolioHistoryStore
    max_portofolio = 100
    
    def __init__(self, file_repository):
        self.file_repository = file_repository

    def add(self, snapshot):
        if len(self._store.history) > self.max_portofolio:
            new_filename = self.file_repository.new_file()
            self._init_inner_store(new_filename)
        
        self._store.add(snapshot)

    def get_last(self):
        return self._store.get_last()

    def save(self):
        self._store.save()
    
    def _init_inner_store(self, filename):
        storage = self.portofolio_history_storage_klass(filename)
        self._store = self.portofolio_history_store_klass(storage)
        self._store.init()

    def init(self):
        latest_filename = self.file_repository.get_latest_filename()
        self._init_inner_store(latest_filename)


class BibitAPI:
    def __init__(self, secret_storage):
        self.secret_storage = secret_storage
    
    def _request(self, method, endpoint, data={}, allow_fail=True):
        headers = {
            'authority': 'api.bibit.id',
            'sec-ch-ua': '"Google Chrome";v="87", " Not;A Brand";v="99", "Chromium";v="87"',
            'accept': 'application/json, text/plain, */*',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.88 Safari/537.36',
            'x-platform': 'web',
            'sec-ch-ua-mobile': '?0',
            'origin': 'https://app.bibit.id',
            'sec-fetch-site': 'same-site',
            'sec-fetch-mode': 'cors',
            'sec-fetch-dest': 'empty',
            'referer': 'https://app.bibit.id/',
            'accept-language': 'en-US,en;q=0.9',
        }

        if self.secret_storage.access_token:
            headers['authorization'] = f'Bearer {self.secret_storage.access_token}'

        res = requests.request(method.upper(), f'https://api.bibit.id{endpoint}', json=data, headers=headers)
        if not allow_fail:
            res.raise_for_status()

        return res

    def _refresh_token(self):
        self.secret_storage.access_token = None

        res = self._request(
            'POST',
            '/auth/token', 
            data={"notifid": "", "refresh_token": self.secret_storage.refresh_token},
            allow_fail=False,
        )

        token = res.json()['data']['token']
        self.secret_storage.access_token = token['access_token']
        self.secret_storage.refresh_token = token['refresh_token']
        self.secret_storage.save()

    def request(self, method, url, data={}):
        res = self._request(method, url, data)
        if res.status_code == 200:
            return res

        if res.status_code == 401:
            self._refresh_token()
            return self._request(method, url, data)

        res.raise_for_status()
         
    def get_portofolio(self):
        return self.request('GET', "/portfolio").json()
        

class TelegramAPI:
    def __init__(self, secret_storage):
        self.secret_storage = secret_storage

    def send_message(self, content):
        to_replace = ['.', '[', ']', '-', '|']
        safe_msg = content
        for c in to_replace:
            safe_msg = safe_msg.replace(c, f'\{c}')

        requests.post(
            f'https://api.telegram.org/bot{self.secret_storage.telegram_token}/sendMessage',
            json={
                "chat_id": self.secret_storage.telegram_chat_id, 
                "text": safe_msg, 
                "parse_mode": "MarkdownV2"
            },
        ).raise_for_status()


class BibitNotifyJob:
    def __init__(self, bibit_api, telegram_api, portofolio_history_store):
        self.bibit_api = bibit_api
        self.telegram_api = telegram_api
        self.portofolio_history_store = portofolio_history_store 

    def _clean_porto_item(self, porto_item):
        fields = ["id", "invested", "marketvalue", "name"]
        return {field: porto_item[field] for field in fields}
    
    def _clean_porto(self, porto):
        return list(map(self._clean_porto_item, porto['result']))

    def _build_porto_map(self, porto):
        return {porto_item['id']: porto_item for porto_item in porto}

    def _format_currency(self, num):
        rounded = round(num)
        return f"{rounded:,}"

    def _format_message(self, name, change_percentage, total, profit):
        if change_percentage > 1e-9:
            emoji = '\U0001F4C8'
        elif change_percentage < -1e-9:
            emoji = '\U0001F4C9'
        else:
            emoji = '\u2b1c'

        change_percentage = abs(change_percentage * 100)
        total = self._format_currency(total)
        profit = self._format_currency(profit)
        return f"{emoji} {change_percentage:.1f} | *{name}*\n{total} [{profit}]\n"

    def _construct_message(self, porto):
        last_porto = self.portofolio_history_store.get_last()
        last_porto_map = self._build_porto_map(last_porto)
        porto_map = self._build_porto_map(porto)

        should_send = False
        message_parts = []
        for porto_id in sorted(porto_map.keys()):
            porto_item = porto_map[porto_id]
            last_porto_item = last_porto_map.get(porto_id, {})
            
            last_value = last_porto_item.get('marketvalue', 0) 
            last_profit = last_value - last_porto_item.get('invested', 0)

            current_invested = porto_item['invested']
            current_value = porto_item['marketvalue']
            current_profit = current_value - current_invested
            change_percentage = 1.0 * (current_profit - last_profit) / current_invested

            if int(current_value) != int(last_value):
                should_send = True

            formatted = self._format_message(porto_item['name'], change_percentage, current_value, current_profit)
            message_parts.append(formatted)

        return should_send, '\n'.join(message_parts)

            
    def run(self):
        portofolio = self.bibit_api.get_portofolio()
        cleaned = self._clean_porto(portofolio['data'])
        should_send, message = self._construct_message(cleaned)
        if should_send:
            self.telegram_api.send_message(message)
            self.portofolio_history_store.add(cleaned)


def get_absolute_path(path):
    file_abspath = os.path.abspath(__file__)
    basedir = os.path.dirname(file_abspath)
    return os.path.join(basedir, path)


def new_secret_store():
    secret_storage = JSONFileStorage(get_absolute_path(".secrets.json"))
    secret_store = SecretStore(secret_storage)
    secret_store.init()
    return secret_store


def new_rolling_portofolio_history_store():
    history_file_repository = RollingJSONFileRepository(get_absolute_path("history"), file_prefix="history")
    history_file_repository.init()

    history_store = RollingPortofolioHistoryStore(history_file_repository)
    history_store.init()
    return history_store


if __name__ == '__main__':
    secret_store = new_secret_store() 
    portofolio_history_store = new_rolling_portofolio_history_store()
    
    bibit_api = BibitAPI(secret_store)
    telegram_api = TelegramAPI(secret_store)
    
    job = BibitNotifyJob(bibit_api, telegram_api, portofolio_history_store)
    job.run()
