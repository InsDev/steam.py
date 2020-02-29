# -*- coding: utf-8 -*-

"""
MIT License

Copyright (c) 2020 James

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import asyncio
import json
import logging
import re
from base64 import b64encode
from collections import OrderedDict
from sys import version_info
from time import time

import aiohttp
import rsa

from . import __version__, errors
from .enums import URL
from .guard import generate_one_time_code, ConfirmationManager
from .state import State
from .trade import Inventory
from .user import ClientUser

log = logging.getLogger(__name__)


async def json_or_text(response):
    text = await response.text(encoding='utf-8')
    if 'application/json' in response.headers['content-type']:  # thanks steam very cool
        return json.loads(text)
    return text


def Route(api, call, version='v1'):
    """Used for formatting API request URLs"""
    return f'{URL.API}/{api}/{call}/{version}'


class HTTPClient:
    """The HTTP Client that interacts with the Steam web API."""
    DATA_REQUEST = '\nDATA: {data}\n'
    PARAMS_REQUEST = '\nPARAMS: {params}\n'

    def __init__(self, loop, session, client):
        self._loop = loop
        self._session: aiohttp.ClientSession = session
        self._client = client
        self._state = State(loop=loop, client=client, http=self)

        self.shared_secret = None
        self.identity_secret = None
        self._one_time_code = None

        self.session_id = None
        self._confirmation_manager = None
        self._steam_id = None
        self._user = None
        self._logged_in = False
        self._user_agent = \
            f'steam.py/{__version__} bot (https://github.com/Gobot1234/steam.py), ' \
            f'Python/{version_info[0]}.{version_info[1]}, aiohttp/{aiohttp.__version__}'

        self._notifications = {
            4: ('comment', self._parse_comment),
            5: ('receive_items', self._parse_item_receive),
            6: ('receive_invite', self._parse_invite_receive),
            8: ('receive_gift', self._parse_item_receive),
        }

    def recreate(self):
        if self._session.closed:
            self._session = aiohttp.ClientSession(loop=self._loop)

    def code(self):
        if self.shared_secret:
            return generate_one_time_code(self.shared_secret)
        else:
            return input('Please enter a Steam guard code\n> ')

    async def request(self, method, url, **kwargs):
        for tries in range(5):
            async with self._session.request(method, url, **kwargs) as r:
                log.debug(f'{method} {url} '
                          f'{"with" if kwargs.get("data") or kwargs.get("params") else ""}'
                          f'{self.DATA_REQUEST.format(data=kwargs.get("data")) if kwargs.get("data") else ""}'
                          f'{self.PARAMS_REQUEST.format(params=kwargs.get("params")) if kwargs.get("params") else ""}'
                          f'has returned {r.status}')
                data = await json_or_text(r)

                if data == 'Access is denied. Retrying will not help. Please verify your <pre>key=</pre> parameter':
                    raise errors.InvalidCredentials('Invalid API key')
                # the request was successful so just return the text/json
                if 300 > r.status >= 200:
                    log.debug(f'{method} {url} has received {data}')
                    return data

                # we are being rate limited
                if r.status == 429:
                    raise errors.TooManyRequests('We are being rate limited try again soon')
                # we've received a 500 or 502, unconditional retry
                if r.status in {500, 502}:
                    await asyncio.sleep(1 + tries * 2)
                    continue

                # the usual error cases
                if r.status == 403:
                    raise errors.Forbidden(r, data)
                elif r.status == 404:
                    raise errors.NotFound(r, data)
                else:
                    raise errors.HTTPException(r, data)

            # we've run out of retries, raise.
        raise errors.HTTPException(r, data)

    async def login(self, username: str, password: str, api_key: str, shared_secret: str, identity_secret: str = None):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.shared_secret = shared_secret
        self.identity_secret = identity_secret

        login_response = await self._send_login_request()
        if 'captcha_needed' in login_response.keys():
            raise errors.LoginError('A captcha code is required, please try again later')

        await self._assert_valid_credentials(login_response)
        await self._perform_redirects(login_response)

        self._logged_in = True
        self._client.dispatch('login')
        data = await self.fetch_profile(login_response['transfer_parameters']['steamid'])
        self._user = ClientUser(state=self._state, data=data)
        self._confirmation_manager = ConfirmationManager(state=self._state)
        self._loop.create_task(self._poll_notifications())

    async def logout(self):
        log.debug('Logging out of session')
        await self._session.get(url=f'{URL.COMMUNITY}/login/logout/')
        await self._session.close()
        self._logged_in = False
        self._client.dispatch('logout')

    async def _send_login_request(self):
        rsa_key, rsa_timestamp = await self._fetch_rsa_params()
        encrypted_password = \
            b64encode(rsa.encrypt(self.password.encode('utf-8'), rsa_key)).decode()
        return await self._send_login(rsa_timestamp, encrypted_password)

    async def _fetch_rsa_params(self, current_repetitions: int = 0) -> tuple:
        maximum_repetitions = 5
        data = {
            'username': self.username,
            'donotcache': int(time() * 1000)
        }
        try:
            key_response = await self.request('POST', url=f'{URL.COMMUNITY}/login/getrsakey/', data=data)
        except Exception as e:
            await self._session.close()
            raise errors.LoginError(e)
        try:
            rsa_mod = int(key_response['publickey_mod'], 16)
            rsa_exp = int(key_response['publickey_exp'], 16)
            rsa_timestamp = key_response['timestamp']
            return rsa.PublicKey(rsa_mod, rsa_exp), rsa_timestamp
        except KeyError:
            if current_repetitions < maximum_repetitions:
                return await self._fetch_rsa_params(current_repetitions + 1)
            else:
                raise ValueError('Could not obtain rsa-key')

    async def _send_login(self, rsa_timestamp: str, encrypted_password: str):
        data = {
            'username': self.username,
            'password': encrypted_password,
            "emailauth": '',
            "emailsteamid": '',
            "twofactorcode": self._one_time_code or '',
            "captchagid": '-1',
            "captcha_text": '',
            "loginfriendlyname": self._user_agent,
            "rsatimestamp": rsa_timestamp,
            "remember_login": True,
            "donotcache": int(time() * 1000),
        }
        try:
            login_response = await self.request('POST', url=f'{URL.COMMUNITY}/login/dologin/', data=data)
            if login_response['requires_twofactor']:
                self._one_time_code = self.code()
                return await self._send_login_request()
            return login_response
        except Exception as e:
            raise errors.HTTPException(e)

    async def _perform_redirects(self, response_dict: dict):
        parameters = response_dict.get('transfer_parameters')
        if parameters is None:
            raise errors.HTTPException('Cannot perform redirects after login, no parameters fetched. '
                                       'The Steam API likely is down, please try again later.')
        for url in response_dict['transfer_urls']:
            await self.request('POST', url=url, data=parameters)

    async def _assert_valid_credentials(self, login_response: dict):
        if not login_response['success']:
            await self._session.close()
            raise errors.InvalidCredentials(login_response['message'])
        home = await self._session.get(url=f'{URL.COMMUNITY}/my/home/')
        self.session_id = re.search(r'g_sessionID = "(?P<sessionID>.*?)";', await home.text()).group('sessionID')

    async def fetch_profile(self, user_id64: int):
        params = {
            "key": self.api_key,
            "steamids": user_id64
        }
        full_resp = await self.request('GET', url=Route('ISteamUser', 'GetPlayerSummaries', 'v2'), params=params)
        resp = full_resp['response']['players'][0]
        return resp if resp else None

    async def fetch_profiles(self, user_id64s: list):
        to_ret = []

        def chunk():  # chunk the list into 100 element sublists for the requests
            for i in range(0, len(user_id64s), 100):
                yield user_id64s[i:i + 100]

        chunked_user_ids = list(chunk())
        for sublist in chunked_user_ids:  # make the requests
            for _ in sublist:
                params = {
                    "key": self.api_key,
                    "steamids": ','.join([user_id for user_id in sublist])
                }

            full_resp = await self.request('GET', Route('ISteamUser', 'GetPlayerSummaries', 'v2'), params=params)
            to_ret.extend([user for user in full_resp['response']['players']])
        return to_ret

    async def add_user(self, user_id64):
        data = {
            "sessionid": self.session_id,
            "steamid": user_id64,
            "accept_invite": 0
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/AddFriendAjax', data=data)

    async def remove_user(self, user_id64):
        data = {
            "sessionid": self.session_id,
            "steamid": user_id64,
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/RemoveFriendAjax', data=data)

    async def block_user(self, user_id64):
        data = {
            "sessionID": self.session_id,
            "steamid": user_id64,
            "block": 1
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/BlockUserAjax', data=data)

    async def unblock_user(self, user_id64):
        data = {
            "sessionID": self.session_id,
            "steamid": user_id64,
            "block": 0
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/BlockUserAjax', data=data)

    async def accept_user_invite(self, user_id64):
        data = {
            "sessionID": self.session_id,
            "steamid": user_id64,
            "accept_invite": 1
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/AddFriendAjax', data=data)

    async def decline_user_invite(self, user_id64):
        data = {
            "sessionID": self.session_id,
            "steamid": user_id64,
            "accept_invite": 0
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/actions/IgnoreFriendInviteAjax', data=data)

    async def post_comment(self, user_id64, comment):
        data = {
            "sessionid": self.session_id,
            "comment": comment,
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/comment/Profile/post/{user_id64}/', data=data)

    async def fetch_user_inventory(self, user_id64, app_id, context_id):
        return await self.request('GET', url=f'{URL.COMMUNITY}/inventory/{user_id64}/{app_id}/{context_id}?count=5000')

    async def fetch_user_escrow(self, url):
        headers = {
            'Referer': f'{URL.COMMUNITY}/tradeoffer/new/?partner={self.id64}',
            'Origin': URL.COMMUNITY
        }
        resp = await self.request('GET', url=url, headers=headers)
        return int(re.search(r'var g_daysTheirEscrow = (?P<escrow>(?:.*?));', resp).group('escrow'))

    async def fetch_friends(self, user_id64):
        params = {
            "key": self.api_key,
            "steamid": user_id64,
            "relationship": 'friend'
        }
        friends = await self.request('GET', url=Route('ISteamUser', 'GetFriendList'), params=params)
        return await self.fetch_profiles([friend['steamid'] for friend in friends['friendslist']['friends']])

    async def _poll_trades(self):
        trades_cache = await self._get_trade_offers()
        await asyncio.sleep(5)
        while 1:
            trades = await self._get_trade_offers()
            if trades != trades_cache:
                for trades_cache, trade in zip(trades_cache, trades):
                    if trades_cache[trades_cache] != trades[trade]:
                        log.debug(f'Received raw trade {trades[trade]}')
                        trade = self._client._store_trade(trades[trade])
                        self._client.dispatch('trade_receive', trade)
                trades_cache = trades
            await asyncio.sleep(5)

    async def _get_trade_offers(self, active_only=True, sent=False, received=True):
        params = {
            "key": self.api_key,
            "active_only": int(active_only),
            "get_sent_offers": int(sent),
            "get_received_offers": int(received)
        }

        try:
            offers = await self.request('GET', url=Route('IEconService', 'GetTradeOffers'), params=params)
        except ValueError:
            await self.login(username=self.username, password=self.password, api_key=self.api_key,
                             shared_secret=self.shared_secret)
            await self._get_trade_offers()
        except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError):
            await self._get_trade_offers()
        else:
            return offers['response']

    async def fetch_trade(self, trade_id):
        params = {
            "key": self.api_key,
            "tradeofferid": trade_id
        }
        request = await self.request('GET', url=Route('IEconService', 'GetTradeOffer'), params=params)
        return request['response']['offer']

    async def accept_user_trade(self, user_id64, trade_id):
        data = {
            'sessionid': self.session_id,
            'tradeofferid': trade_id,
            'serverid': 1,
            'partner': user_id64,
            'captcha': ''
        }
        headers = {'Referer': f'{URL.COMMUNITY}/tradeoffer/{trade_id}'}

        resp = await self.request('POST', url=f'{URL.COMMUNITY}/tradeoffer/{trade_id}/accept',
                                  data=data, headers=headers)
        if resp.get('needs_mobile_confirmation', False):
            if self.identity_secret:
                await asyncio.sleep(2)
                for tries in range(3):
                    try:
                        conf = await self._confirmation_manager.get_trade_confirmation(trade_id)
                    except errors.SteamAuthenticatorError:
                        raise
                    resp = await conf.confirm()
                    if isinstance(resp, dict):
                        return conf
                    log.debug(f'Failed to except the trade #{trade_id}, with the error:\n{resp}')
                    raise errors.SteamAuthenticatorError('Failed to except the trade, see the log for the response')
                raise errors.ClientException("Couldn't find a matching confirmation")
            else:
                raise errors.ClientException('Accepting trades requires an identity_secret')
        return resp

    async def decline_user_trade(self, trade_id):
        data = {
            "sessionid": self.session_id
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/tradeoffer/{trade_id}/decline', data=data)

    async def cancel_user_trade(self, trade_id):
        data = {
            "sessionid": self.session_id
        }
        return await self.request('POST', url=f'{URL.COMMUNITY}/tradeoffer/{trade_id}/cancel', data=data)

    async def fetch_user_items(self, user_id64, assets):
        items = []
        app_ids = list(OrderedDict.fromkeys([item['appid'] for item in assets]))  # remove duplicate app_ids
        context_ids = list(OrderedDict.fromkeys([item['contextid'] for item in assets]))  # and ctx_ids
        for app_id, context_id in zip(app_ids, context_ids):
            inv = await self.fetch_user_inventory(user_id64, app_id, context_id)
            inventory = Inventory(state=self._state, data=inv, owner=await self._client.fetch_user(user_id64))
            items.extend(inventory.items)
        return items

    async def send_trade_offer(self, user_id64, user_id, to_send, to_receive, offer_message):
        data = {
            "sessionid": self.session_id,
            "serverid": 1,
            "partner": user_id64,
            "tradeoffermessage": offer_message,
            "json_tradeoffer": json.dumps({
                "newversion": True,
                "version": 4,
                "me": {
                    "assets": [dict(item) for item in to_send] if to_send else [],
                    "currency": [],
                    "ready": False
                },
                "them": {
                    "assets": [dict(item) for item in to_receive] if to_receive else [],
                    "currency": [],
                    "ready": False
                }
            }),
            "captcha": '',
            "trade_offer_create_params": {}
        }
        headers = {
            'Referer': f'{URL.COMMUNITY}/tradeoffer/new/?partner={user_id}',
            'Origin': URL.COMMUNITY
        }
        post = await self.request('POST', url=f'{URL.COMMUNITY}/tradeoffer/new/send', data=data, headers=headers)
        return post