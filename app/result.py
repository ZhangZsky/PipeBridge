"""统一返回结果构造器"""


class Result:
    """统一的 API 返回结果类型，提供 ok / fail 两个静态方法"""

    @staticmethod
    def ok(data=None, **extra):
        """构造成功结果

        Args:
            data: 返回数据，为 None 时省略 data 字段
            **extra: 额外字段，合并到结果字典中
        """
        result = {'success': True}
        if data is not None:
            result['data'] = data
        result.update(extra)
        return result

    @staticmethod
    def fail(error=None, **extra):
        """构造失败结果

        Args:
            error: 错误信息，为 None 时省略 error 字段
            **extra: 额外字段，合并到结果字典中
        """
        result = {'success': False}
        if error is not None:
            result['error'] = error
        result.update(extra)
        return result
