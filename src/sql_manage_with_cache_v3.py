import time
import inspect
from collections import OrderedDict
import sys
import warnings
import random
from threading import Thread,RLock
import json
import hashlib
import heapq

import pymysql


# 查询唯一标识结构体
class QueryStruct:
    def __init__(self,host:str,db:str,query:str,is_encryption = False):
        # 主机名、数据库名、查询语句
        self.__host = host
        self.__db = db
        self.__query = query
        self.is_encryption = is_encryption
    # MD5加密
    def encryption(self,key:str):
        # MD5加密生成key
        return hashlib.md5(key.encode()).hexdigest()
    # 重写类比较
    def __eq__(self, other) -> bool:
        return (self.__host == other.__host) and (self.__db == other.__db)\
             and (self.__query == other.__query)
    # 重写hash
    def __hash__(self) -> int:
        return hash((self.__host,self.__db,self.__query))
    # 重写print行为
    def __str__(self):
        p_str = f"{self.__host}/{self.__db}: {self.__query}"
        if self.is_encryption:
            p_str = self.encryption(p_str)
        return p_str

# 安全LRU算法
class LRU:
    def __init__(self, capacity = 128,to_json = True):
        self.capacity = capacity
        self.cache = OrderedDict()
        self.to_json = to_json
        self._lock = RLock()
    def _query(self,key:QueryStruct,discard = False):
        '''加锁安全查询'''
        if key not in self.cache:
            return
        with self._lock:
            if key in self.cache:
                value = self.cache.pop(key)
                if not discard:
                    self.cache[key] = value
                    return value
                else:
                    print(f"数据{key}已被删除！")
    def put(self, key, value):
        _value = self._query(key)
        if _value is not None:
            return
        if len(self.cache) >= self.capacity:
            # 若缓存已满，则需要淘汰最早没有使用的数据
            _ = self.cache.popitem(last=False)[0]
            print(f"缓存已满,淘汰最早没有使用的数据{key}!")
        # 录入缓存
        if self.to_json:
            value = (json.dumps(value[0]),value[1])
        self.cache[key]=value
        
    def discard(self,key):
        return self._query(key,discard=True)
    # 热点数据前移
    def query(self,key):
        value = self._query(key)
        if value is not None and self.to_json:
            value = (json.loads(value[0]),value[1])
        return value

class QueryInfo(LRU):
    '''
    缓存类
    _get_info: 取缓存数据
    _set_info: 存缓存数据
    _delaydel: 设置过期时间,延时删除
    
    '''
    def __init__(self,capacity = 10000,to_json = False):
        super(QueryInfo,self).__init__(capacity=capacity,to_json=to_json)
        # 默认停留时间为3天
        self.default_nx = 60 * 60 * 24 * 3
        self.tick = 1
        self.keep_alive = True
        # 监控线程,每tick秒删除过期key值
        self.check_thread = None
        # 小根堆,节点为(e_time,key)
        self.heap = []

    def _get_info(self,key:QueryStruct):
        '''
        查找键值
        '''
        res = self.query(key)
        if res:
            return res[0]

    def _set_info(self,key:QueryStruct,value,
                    nx = None,
                    each_memory = 10):
        '''
        nx 默认过期时间为3天
        each_memory: 默认每次插入的变量内存不大于10M
        '''
        memory = sys.getsizeof(value)/(1024**2)
        if memory > each_memory:
            warnings.warn(f'变量内存超过{each_memory}M,将不会写入缓存！')
            return
        if nx:
            e_time = time.time() + nx
        else:
            e_time = time.time() + self.default_nx
        self.put(key,(value,e_time))
        heapq.heappush(self.heap,(e_time,key))
    def _clear(self,shut_down_thread:bool):
        if shut_down_thread:
            # 确保线程被关闭
            self.keep_alive = False
            dp = self.tick
            while self.check_thread and self.check_thread.is_alive():
                time.sleep(dp)
                dp /= 2
            print("回收监控线程成功！")
        self.heap.clear()
        self.cache.clear()

    def check_e_time(self):
        '''
        依次查找堆顶,删除过期的键值对
        '''
        while self.keep_alive:
            if len(self.heap) > 3 * self.capacity:
                # 数据偏离太大,利用缓存中的键值重新建堆,O(n)
                with self._lock:
                    # 加锁,防止遍历字典时字典被更新
                    tmp = [(v[1],k) for k,v in self.cache.items()]
                    heapq.heapify(tmp)
                    self.heap = tmp
            f_time = time.time()
            while self.heap:
                # 堆顶为最小值
                item = self.heap[0]
                # 最小值也大于当前时间戳,说明没过期的内存,O(klogn)
                if item[0] > f_time:
                    break
                heapq.heappop(self.heap)
                self.discard(item[1])
            time.sleep(self.tick)
    def start_check_thread(self):
        self.keep_alive = True
        if self.check_thread and self.check_thread.is_alive():
            return
        with self._lock:
            if (self.check_thread is None) or (not self.check_thread.is_alive()):
                self.check_thread = None
                self.check_thread = Thread(target=self.check_e_time)
                self.check_thread.setDaemon(daemonic=True)
                self.check_thread.start()
                print("启动监控线程！")

# 缓存结构
class Query:
    # 此类调用内部类QueryInfo,同时__call__实现装饰器功能
    def __init__(self,capacity = 1,nx = (6,12)):
        self.query_info = QueryInfo(capacity=capacity,
                                    to_json = False)
        self.cache_enable = True
        self.start_cache()
        if nx[0] > nx[1]:
            raise ValueError('起始时间必须小于终止时间！')
        self.nx = nx
    def check_query(self,query:str):
        querys = query.split(';')
        for que in querys:
            substr = que[:25].lower()
            if substr.startswith(('insert','update','delete','alter','replace')):
                return False
            elif substr.startswith('create'):
                if not substr.startswith('create temporary table'):
                    return False 
        return True
    def _lower(self,query):
        lower_query = ''
        active = False
        for c in query:
            if c == "'":
                active = ~active
            elif c >= 'A' and c <= 'Z':
                if not active:
                    c = chr(ord(c) + 32)
            lower_query += c
            
        return lower_query
    def gc_cache(self,only_clear_cache = True):
        # 回收线程和清除内存接口
        if only_clear_cache:
            self.query_info._clear(shut_down_thread = False)
        else:
            self.cache_enable = False
            self.query_info._clear(shut_down_thread=True)
    def start_cache(self):
        # 启用缓存功能
        self.cache_enable = True
        self.query_info.start_check_thread()
    def __call__(self, fun):
        def wrapper(query:str,
                    host = 'localhost',
                    user = 'root',
                    password = '123456',
                    db = 'sj',
                    args = None,
                    return_dict: bool = False,
                    autocommit: bool = False,
                    muti_query: bool = False,
                    ex_many_mode: bool = False):
            if self.cache_enable:
                data = None
                flag = self.check_query(query)
                low_query = self._lower(query)
                if flag:
                    if args is not None:
                        low_query = low_query % args
                    data = self.query_info._get_info(QueryStruct(host,db,low_query))
                if data is not None:
                    # 命中缓存，直接返回结果
                    print(f"命中缓存 -> {host}/{db}: {query}")
                    return data       
                # 查询数据库
                data = fun(query,host,user,password,db,args,
                        return_dict,autocommit,muti_query,ex_many_mode)
                if flag:
                    # 将查询结果加入缓存,nx为过期时间,随机值0.5h~5h
                    nx = random.randint(*self.nx)
                    self.query_info._set_info(QueryStruct(host,db,low_query), data, nx = nx)
            else:
                data = fun(query,host,user,password,db,args,
                        return_dict,autocommit,muti_query,ex_many_mode)
            return data
        return wrapper

# 耗时统计装饰器
def timeit(fun):
    def wrapper(*arg,**kwarg):
        start_time = time.time()
        res = fun(*arg,**kwarg)
        end_time = time.time()
        print(f'执行时间：{end_time - start_time:.5f}s')
        return res
      
    return wrapper

# 缓存中间件
ex_fun = Query(capacity=10000,nx = (1800,18000))

@timeit
@ex_fun
def run_sql_query(query,
                  host = 'localhost',
                  user = 'root',
                  password = '123456',
                  db = 'sj',
                  args = None,
                  return_dict: bool = False,
                  autocommit: bool = True,
                  muti_query: bool = False,
                  ex_many_mode: bool = False
                  ):
    '''
    :param args: 输入参数,在insert或者防止sql注入时使用
    :param return_dict: 是否以字典形式返回
    :param muti_query: 是否同时执行多条sql语句,多条语句以;号隔开
    :param autocommit: 是否自动commit
    :param ex_many_mode: 是否启用批量插入，测试args应为列表
    '''
    
    cursorclass = pymysql.cursors.DictCursor if return_dict else pymysql.cursors.Cursor
    client_flag = pymysql.constants.CLIENT.MULTI_STATEMENTS if muti_query else 0
    conn = pymysql.connect(host=host,
                           user=user,
                           password=password,
                           db=db,
                           charset='utf8',
                           cursorclass=cursorclass,
                           client_flag=client_flag,
                           autocommit=autocommit)
    cursor = conn.cursor()
    data = []
    try:
        if ex_many_mode:
            print(f'执行sql: {cursor.mogrify(query,args[0])}...')
            cursor.executemany(query,args)
        else:
            print(f'执行sql: {cursor.mogrify(query,args)}')
            cursor.execute(query,args)
        data.append(cursor.fetchall())
        while cursor.nextset():
            data.append(cursor.fetchall())
    except Exception as e:
        conn.rollback()
        raise pymysql.err.ProgrammingError(f'执行{inspect.stack()[0][3]}方法出现错误，错误代码：{e}')
    finally:
        cursor.close()
        conn.close()
    # 兼容原来的单条查询格式
    if len(data) == 1:
        data = data[0]
    return data
if __name__ == "__main__":
    _ = run_sql_query("select * from test")
    print(_)
    _ = run_sql_query("select * from test")
    print(_)
    _ = run_sql_query("select id from test")
    _ = run_sql_query("select * from test")

    ex_fun.gc_cache(only_clear_cache=False)

    _ = run_sql_query("select * from test")
    _ = run_sql_query("select * from test")

    ex_fun.start_cache()

    _ = run_sql_query("select * from test")
    time.sleep(15)
    _ = run_sql_query("select * from test")
    print(_)
    _ = run_sql_query("select * from test")
    print(_)
    
    # ex_fun.cache_enable = False
    
    # _ = run_sql_query("select * from test",db ='sj')
    # _ = run_sql_query("select * from test",db ='sj')
