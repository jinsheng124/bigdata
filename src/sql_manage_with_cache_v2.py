import time
import inspect
import hashlib
from collections import OrderedDict
import sys
import warnings
import random
from threading import Thread
import os

import pymysql


# 查询唯一标识结构体
class QueryStruct:
    def __init__(self,host:str,db:str,query:str):
        # 主机名、数据库名、查询语句
        self.host = host
        self.db = db
        self.query = query
    def __call__(self):
        # MD5加密生成key
        key = str(self.db) + str(self.host) + str(self.query)
        key = hashlib.md5(key.encode()).hexdigest()
        return key

# LRU算法
class LRU:
    def __init__(self, capacity = 128):
        self.capacity = capacity
        self.cache = OrderedDict()
 
    def put(self, key, value):
        
        if key in self.cache:
            # 若数据已存在，表示命中一次，需要把数据移到缓存队列末端
            self.cache.move_to_end(key)
            return
        if len(self.cache) >= self.capacity:
            # 若缓存已满，则需要淘汰最早没有使用的数据
            _ = self.cache.popitem(last=False)
            print(f"缓存已满,淘汰最早没有使用的数据{_[0]}!")
            return _[0]
        # 录入缓存
        self.cache[key]=value
        
    def dels(self,key):
        if key in self.cache:
            _ = self.cache.pop(key)
            print(f"数据{key}已被删除！")
            return _[0]
    # 热点数据前移
    def query(self,key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]

class QueryInfo:
    '''
    缓存类
    _get_info: 取缓存数据
    _set_info: 存缓存数据
    _delaydel: 设置过期时间,延时删除
    
    '''
    def __init__(self,max_size = 1000):
        self.lru_cache = LRU(capacity=max_size)
        # 默认停留时间为1个月
        self._nx = 60 * 60 * 24 * 3
        self.heartbeat = 3
        self.e_time_pool = []
        self.keep_alive = True
        # 启动线程
        self.check_thread = Thread(target=self.check_e_time)
        self.start_check_thread()

    def _get_info(self,query:QueryStruct):
        '''
        OrderDict结构：key ->(value,e_time)
        e_time为过期时刻,若当前时刻大于过期时刻,回收key并返回,否则返回value
        '''
        key = query()
        res = self.lru_cache.query(key)
        return res

    def _set_info(self,query:QueryStruct,value,
                    nx = None,
                    each_memory = 2):
        '''
        nx 默认不设过期时间
        each_memory: 默认每次插入的变量内存不大于2M
        若成功插入数据,并启动延时删除线程
        '''
        key = query()
        memory = sys.getsizeof(value)/(1024**2)
        if memory > each_memory:
            warnings.warn(f'变量内存超过{each_memory}M,将不会写入缓存！')
            return
        if nx:
            e_time = time.time() + nx
        else:
            e_time = time.time() + self._nx
        self.lru_cache.put(key,value)

        # 二分插入
        low = 0
        high = len(self.e_time_pool)-1
        while low <= high:
            mid = (low + high)//2
            if self.e_time_pool[mid][1] < e_time:
                low = mid + 1
            elif self.e_time_pool[mid][1] > e_time:
                high = mid - 1
            else:
                break
        self.e_time_pool.insert(low,(key,e_time))
    def _clear(self,):
        self.keep_alive = False
        self.e_time_pool.clear()
        self.lru_cache.cache.clear()
    def check_e_time(self):
        while self.keep_alive:
            f_time = time.time()
            while len(self.e_time_pool) > 0:
                key,e_time = self.e_time_pool[0]
                if f_time < e_time:
                    break
                self.e_time_pool.pop(0)
                self.lru_cache.dels(key)
            time.sleep(self.heartbeat)
    def start_check_thread(self):
        if self.check_thread:
            self.check_thread.start()
        else:
            print('打开监控线程失败！')

# 缓存结构
class Query:
    # 此类调用内部类QueryInfo,同时__call__实现装饰器功能
    def __init__(self,max_size = 1000,nx = (1800,18000)):
        self.query_info = QueryInfo(max_size=max_size)
        self.cache_enable = True
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
    def clearcache(self):
        self.query_info._clear()         
    def __call__(self, fun):
        def wrapper(query:str,
                    host = 'localhost',
                    user = 'root',
                    password = '123456',
                    db = 'ref_bigdata',
                    args = None,
                    return_dict: bool = False,
                    autocommit: bool = False,
                    muti_query: bool = False,
                    ex_many_mode: bool = False):
            if self.cache_enable:
                self.query_info.keep_alive = True
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
                # 清除缓存
                self.clearcache()
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
ex_fun = Query(max_size=20000,nx = (1800,18000))

@timeit
@ex_fun
def run_sql_query(query,
                  host = 'localhost',
                  user = 'root',
                  password = '123456',
                  db = 'ref_bigdata',
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
        cursor.close()
        conn.close()
        raise pymysql.err.ProgrammingError(f'执行{inspect.stack()[0][3]}方法出现错误，错误代码：{e}')
    cursor.close()
    conn.close()
    # 兼容原来的单条查询格式
    if len(data) == 1:
        data = data[0]
    return data
if __name__ == "__main__":
    _ = run_sql_query("select * from test",db ='sj')
    _ = run_sql_query("select * from test",db ='sj')
    
    # ex_fun.cache_enable = False
    
    # _ = run_sql_query("select * from test",db ='sj')
    # _ = run_sql_query("select * from test",db ='sj')