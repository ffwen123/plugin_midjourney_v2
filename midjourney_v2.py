#!/usr/bin/env python
# -*- coding=utf-8 -*-
"""
@time: 2023/5/25 10:46
@Project ：chatgpt-on-wechat
@file: midjourney_v2.py
"""
import json
import os
import random
import string
import time
import unicodedata
import requests
import base64
import oss2
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from bridge.bridge import Bridge
from channel.wechatcom.wechatcomapp_channel import WechatComAppChannel
from config import conf
import plugins
from plugins import *
from common.log import logger
from common.expired_dict import ExpiredDict

comapp = WechatComAppChannel()


def is_chinese(prompt):
    for char in prompt:
        if char in ["\r", "\t", "\n"]:
            continue
        if "CJK" in unicodedata.name(char):
            return True
    return False


@plugins.register(name="MidjourneyV2", desc="用midjourney api来画图", desire_priority=1, version="0.1",
                  author="ffwen123")
class MidjourneyV2(Plugin):
    def __init__(self):
        super().__init__()
        curdir = os.path.dirname(__file__)
        config_path = os.path.join(curdir, "config.json")
        self.params_cache = ExpiredDict(60 * 60)
        self.agent_id = conf().get("wechatcomapp_agent_id")
        if not os.path.exists(config_path):
            logger.info('[RP] 配置文件不存在，将使用config.json.template模板')
            config_path = os.path.join(curdir, "config.json.template")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self.api_url = config["api_url"]
                self.call_back_url = config["call_back_url"]
                self.submit_uv = config["submit_uv"]
                self.point_uv = config["point_uv"]
                self.button_data = config["button_data"]
                self.rule = config["rule"]
                self.oss_conf = config["oss_conf"]
                auth = oss2.Auth(self.oss_conf["akid"], self.oss_conf["akst"])
                self.bucket_img = oss2.Bucket(auth, self.oss_conf["aked"], self.oss_conf["bucket_name"])
                self.default_params = config["defaults"]
                if not self.api_url or "你的API" in self.api_url:
                    raise Exception("please set your Midjourney api in config or environment variable.")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            logger.info("[RP] inited")
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                logger.warn(f"[RP] init failed, config.json not found.")
            else:
                logger.warn("[RP] init failed." + str(e))
            raise e

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type not in [ContextType.IMAGE_CREATE, ContextType.IMAGE]:
            return

        logger.info("[RP] image_query={}".format(e_context['context'].content))
        reply = Reply()
        try:
            user_id = e_context['context']["session_id"]
            content = e_context['context'].content[:]
            if e_context['context'].type == ContextType.IMAGE_CREATE:
                # 解析用户输入 如"mj [img2img] prompt --v 5 --ar 3:2" /variation:进行放大或重新生成
                if content.find("—") >= 0:
                    content = content.replace("—", "--")
                if content.find("--") >= 0:
                    prompt, commands = content.split("--", 1)
                    commands = " --" + commands
                else:
                    prompt, commands = content, ""
                if "help" in content or "帮助" in content:
                    reply.type = ReplyType.INFO
                    reply.content = self.get_help_text(verbose=True)
                else:
                    flag = False
                    if self.rule.get("image") in prompt:
                        flag = True
                        prompt = prompt.replace(self.rule.get("image"), "")
                    if self.button_data in prompt:
                        submit_uv = prompt.replace(self.button_data, "").strip().split()
                        logger.info("[RP] submit_uv post_json={}".format(" ".join(submit_uv)))
                        if submit_uv[-1] in ["U1", "U2", "U3", "U4", "V1", "V2", "V3", "V4"]:
                            http_resp, messageId = self.get_imageurl(url=self.submit_uv,
                                                                     data={"content": " ".join(submit_uv)})
                            if messageId:
                                if http_resp.get("imageUrl"):
                                    if submit_uv[-1] in ["V1", "V2", "V3", "V4"]:
                                        try:
                                            com_reply = Reply()
                                            com_reply.type = ReplyType.IMAGE_URL
                                            com_reply.content = http_resp.get("imageUrl")
                                            comapp.send(com_reply, e_context['context'])
                                        except Exception as e:
                                            print(e)
                                        time.sleep(2)
                                        reply.type = ReplyType.TEXT
                                        reply.content = self.point_uv.format(messageId)
                                    else:
                                        reply.type = ReplyType.IMAGE_URL
                                        reply.content = http_resp.get("imageUrl")
                                else:
                                    reply.type = ReplyType.ERROR
                                    reply.content = "图片imageUrl为空"
                            else:
                                reply.type = ReplyType.ERROR
                                reply.content = http_resp
                                e_context['reply'] = reply
                                logger.error("[RP] Midjourney API api_data: %s " % http_resp)
                        else:
                            reply.type = ReplyType.INFO
                            reply.content = self.button_data + " 参数错误"
                    else:
                        if is_chinese(prompt):
                            prompt = Bridge().fetch_translate(prompt, to_lang="en") + commands
                        else:
                            prompt += commands
                        params = {**self.default_params}
                        if params.get("prompt", ""):
                            params["prompt"] += f", {prompt}"
                        else:
                            params["prompt"] += f"{prompt}"
                        logger.info("[RP] params={}".format(params))
                        if flag:
                            self.params_cache[user_id] = params
                            reply.type = ReplyType.INFO
                            reply.content = "请发送一张图片给我"
                        else:
                            logger.info("[RP] txt2img params={}".format(params))
                            # 调用midjourneyv2 api来画图
                            http_resp, messageId = self.get_imageurl(url=self.api_url, data=params)
                            if messageId:
                                if http_resp.get("imageUrl"):
                                    try:
                                        com_reply = Reply()
                                        com_reply.type = ReplyType.IMAGE_URL
                                        com_reply.content = http_resp.get("imageUrl")
                                        comapp.send(com_reply, e_context['context'])
                                    except Exception as e:
                                        print(e)
                                    time.sleep(2)
                                    reply.type = ReplyType.TEXT
                                    reply.content = self.point_uv.format(messageId)
                                else:
                                    reply.type = ReplyType.ERROR
                                    reply.content = "图片imageUrl为空"
                            else:
                                reply.type = ReplyType.ERROR
                                reply.content = http_resp
                                e_context['reply'] = reply
                                logger.error("[RP] Midjourney API api_data: %s " % http_resp)
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑
                    e_context['reply'] = reply
            else:
                cmsg = e_context['context']['msg']
                if user_id in self.params_cache:
                    img_params = self.params_cache[user_id]
                    del self.params_cache[user_id]
                    cmsg.prepare()
                    # img_data = open(content, "rb")
                    img_data = base64.b64encode(open(content, "rb").read()).decode('utf-8')
                    # rand_str = "".join(random.sample(string.ascii_letters + string.digits, 8))
                    # num_str = str(random.uniform(1, 10)).split(".")[-1]
                    # filename = f"{rand_str}_{num_str}" + ".png"
                    # oss_imgurl = self.put_oss_image(filename, img_data)
                    # if oss_imgurl:
                    # img_params.update({"prompt": f'''"cmd":"{oss_imgurl} {img_params["prompt"]}"'''})
                    img_params.update({"base64": f"data:image/png;base64,{img_data}", "prompt": img_params["prompt"]})
                    logger.info("[RP] img2img img_post={}".format(img_params))
                    # 调用midjourney api图生图
                    http_resp, messageId = self.get_imageurl(url=self.api_url, data=img_params)
                    if messageId:
                        if http_resp.get("imageUrl"):
                            try:
                                com_reply = Reply()
                                com_reply.type = ReplyType.IMAGE_URL
                                com_reply.content = http_resp.get("imageUrl")
                                comapp.send(com_reply, e_context['context'])
                            except Exception as e:
                                print(e)
                            time.sleep(2)
                            reply.type = ReplyType.TEXT
                            reply.content = self.point_uv.format(messageId)
                        else:
                            reply.type = ReplyType.ERROR
                            reply.content = "图片imageUrl为空"
                    else:
                        reply.type = ReplyType.ERROR
                        reply.content = http_resp
                        e_context['reply'] = reply
                        logger.error("[RP] Midjourney API api_data: %s " % http_resp)
                    # else:
                    #     reply.type = ReplyType.ERROR
                    #     reply.content = "oss上传图片失败"
                    #     e_context['reply'] = reply
                    #     logger.error("[RP] oss2 image result: oss上传图片失败")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑
        except Exception as e:
            reply.type = ReplyType.ERROR
            reply.content = "[RP] " + str(e)
            e_context['reply'] = reply
            logger.exception("[RP] exception: %s" % e)
            e_context.action = EventAction.CONTINUE

    def get_help_text(self, verbose=False, **kwargs):
        if not conf().get('image_create_prefix'):
            return "画图功能未启用"
        else:
            trigger = conf()['image_create_prefix'][0]
        help_text = "利用midjourney api来画图。\n"
        if not verbose:
            return help_text
        help_text += f"使用方法:\n使用\"{trigger}[关键词1] [关键词2]...:提示语\"的格式作画，如\"{trigger}二次元:girl\"\n"
        return help_text

    def get_imageurl(self, url, data):
        api_data = requests.post(url=url, json=data, timeout=120.05)
        if api_data.status_code != 200:
            time.sleep(2)
            api_data = requests.post(url=url, json=data, timeout=120.05)
        if api_data.status_code == 200:
            # 调用回调的URL
            messageId = api_data.json().get("result")
            logger.info("[RP] api_data={}".format(api_data.text))
            if api_data.status_code == 200:
                if api_data.json().get("code", 0) != 1:
                    time.sleep(20)
                time.sleep(10)
                get_resp = requests.get(url=self.call_back_url.format(messageId), timeout=120.05)
                get_time = time.time()
                while not get_resp.text:
                    if time.time() - get_time > 300:
                        break
                    time.sleep(10)
                    get_resp = requests.get(url=self.call_back_url.format(messageId), timeout=120.05)
                if not get_resp.text:
                    return "已失效", None
                out_time = time.time()
                logger.info("[RP] get_resp={}".format(get_resp.text))
                # Webhook URL的响应慢，没隔 5 秒获取一次，超过600秒判断没有结果
                if get_resp.status_code == 200:
                    while get_resp.status_code == 200:
                        _resp = get_resp.json()
                        if _resp.get("status") in ["IN_PROGRESS", "SUBMITTED"]:
                            if time.time() - out_time > 600:
                                break
                            time.sleep(5)
                            get_resp = requests.get(url=self.call_back_url.format(messageId), timeout=120.05)
                        elif _resp.get("status") == "NOT_START":
                            if time.time() - out_time > 600:
                                break
                            time.sleep(20)
                            get_resp = requests.get(url=self.call_back_url.format(messageId), timeout=120.05)
                        else:
                            break
                    logger.info("[RP] get_imageUrl={}".format(get_resp.text))
                    if "imageUrl" in get_resp.content.decode("utf-8"):
                        return get_resp.json(), messageId
                    else:
                        return get_resp.text, None
                else:
                    return "图片URL获取失败", None
        else:
            if "Request Entity Too Large" in api_data.content.decode("utf-8"):
                return "上传图片太大", None
            else:
                return api_data.text, None

    def put_oss_image(self, data_name, img_bytes):
        try:
            _result = self.bucket_img.put_object(self.oss_conf["image_addre"] + data_name, img_bytes)
        except Exception as e:
            print(e)
            try:
                time.sleep(3)
                _result = self.bucket_img.put_object(self.oss_conf["image_addre"] + data_name, img_bytes)
            except Exception as e:
                return None
        print("_result: ", _result)
        return self.oss_conf["image_url"].format(data_name)
