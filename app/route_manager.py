import logging
from utils import (run_command, pw_dump, find_pw_node, get_node_id_by_name,
                   get_node_name_by_id, get_prop_with_fallback, find_device_props,
                   _find_pw_links, _get_ports_for_node, _build_link_info,
                   pw_dump_invalidate)
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaBridgeError

logger = logging.getLogger('MediaBridge')


def _find_ports(pw_data, node_id, direction):
    """查找指定节点的所有端口，返回 [{id, direction, audio_channel, node_id}]"""
    dir_map = {'out': 'output', 'in': 'input'}
    pw_direction = dir_map.get(direction, direction)
    ports = _get_ports_for_node(pw_data, node_id, pw_direction)
    result = []
    for obj in ports:
        info = obj.get('info', {})
        props = info.get('props', {})
        result.append({
            'id': obj.get('id'),
            'direction': direction,
            'audio_channel': props.get('audio.channel', ''),
            'node_id': node_id,
        })
    return result


# 查找与指定节点相关的所有 Link 对象
def _find_links_for_node(pw_data, node_id):
    # 先收集该节点的所有端口 ID
    port_ids = set()
    for port_obj in _get_ports_for_node(pw_data, node_id):
        port_ids.add(port_obj.get('id'))

    links = []
    for link_obj in _find_pw_links(pw_data):
        info = link_obj.get('info', {})
        output_port = info.get('output-port-id')
        input_port = info.get('input-port-id')
        if output_port in port_ids or input_port in port_ids:
            links.append(link_obj)
    return links


def unlink_stream(stream_node_id, link_id=None):
    """断开流的链接，指定 link_id 断开特定链接，否则断开所有"""
    try:
        try:
            stream_node_id = int(stream_node_id)
        except (TypeError, ValueError):
            raise InvalidParamError('无效的流ID')

        if link_id is not None:
            try:
                link_id = int(link_id)
            except (TypeError, ValueError):
                raise InvalidParamError('无效的链接ID')

        if link_id is not None:
            result = run_command(f"pw-cli unlink {link_id}", timeout=5)
            if result['success']:
                logger.info(f"已断开链接: {link_id}")
                pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据
                return {'unlinked': [link_id]}
            raise CommandError(f'断开链接 {link_id} 失败: {result.get("stderr", "")}')

        # 断开该节点的所有链接
        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        links = _find_links_for_node(pw_data, stream_node_id)
        if not links:
            return {'unlinked': [], 'message': '该流无活跃链接'}

        unlinked = []
        failed = []
        for link_obj in links:
            lid = link_obj.get('id')
            if lid is None:
                continue
            result = run_command(f"pw-cli unlink {lid}", timeout=5)
            if result['success']:
                unlinked.append(lid)
                logger.info(f"已断开链接: {lid}")
            else:
                failed.append(lid)
                logger.warning(f"断开链接 {lid} 失败: {result.get('stderr', '')}")

        if failed:
            pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据
            return {'unlinked': unlinked, 'failed': failed}

        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据
        return {'unlinked': unlinked}

    except MediaBridgeError:
        raise
    except Exception as e:
        logger.error(f"断开流链接失败: {e}")
        raise CommandError(str(e)) from e


def get_video_streams():
    """查询所有活跃视频流"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        streams = []
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            info = obj.get('info', {})
            props = info.get('props', {})
            media_class = props.get('media.class', '')
            if not media_class.startswith('Video/'):
                continue

            node_id = obj.get('id')
            name = props.get('node.name', '')
            friendly_name = (props.get('node.description', '')
                             or props.get('node.nick', '')
                             or name)
            application = props.get('application.name', '')

            # 查找该视频流的所有 Link
            links = _find_links_for_node(pw_data, node_id)
            link_infos = [_build_link_info(l, pw_data) for l in links]

            # 收集连接的输出名称
            connected_outputs = []
            for li in link_infos:
                if 'Sink' in media_class:
                    out_node_id = li.get('output_node_id')
                else:
                    out_node_id = li.get('input_node_id')
                if out_node_id:
                    out_name = get_node_name_by_id(out_node_id)
                    if out_name and out_name not in connected_outputs:
                        connected_outputs.append(out_name)

            stream_links = []
            for li in link_infos:
                stream_links.append({
                    'link_id': li['link_id'],
                    'output_port': li['output_port'],
                    'input_port': li['input_port'],
                    'connected_node_id': li.get('input_node_id') or li.get('output_node_id'),
                    'connected_node_name': li.get('input_node_name') or li.get('output_node_name'),
                })

            streams.append({
                'node_id': node_id,
                'name': name,
                'friendly_name': friendly_name,
                'application': application,
                'media_class': media_class,
                'connected_outputs': connected_outputs,
                'links': stream_links,
            })

        return streams

    except MediaBridgeError:
        raise
    except Exception as e:
        logger.error(f"获取视频流失败: {e}")
        raise CommandError(str(e)) from e


def route_video_stream(stream_node_id, target_output_name):
    """将视频流路由到指定视频输出"""
    try:
        try:
            stream_node_id = int(stream_node_id)
        except (TypeError, ValueError):
            raise InvalidParamError('无效的流ID')

        if not target_output_name:
            raise InvalidParamError('参数不完整：需要 target_output_name')

        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        # 查找视频流节点
        stream_node = find_pw_node(pw_data, node_id=stream_node_id)
        if not stream_node:
            raise DeviceNotFoundError(f'未找到视频流节点 ID: {stream_node_id}')

        # 查找目标输出节点
        target_node = find_pw_node(pw_data, name=target_output_name)
        if not target_node:
            # DRM 显示设备没有 PipeWire 节点，无法通过 PipeWire 路由
            # 检查是否为 DRM 设备名
            if target_output_name.startswith('card') or 'HDMI' in target_output_name.upper() or 'DP' in target_output_name.upper():
                return {
                    'stream_node_id': stream_node_id,
                    'target_output_name': target_output_name,
                    'message': f'DRM 显示设备 {target_output_name} 不支持 PipeWire 路由，请使用显示输出配置 API',
                }
            raise DeviceNotFoundError(f'未找到视频输出: {target_output_name}')

        target_node_id = target_node.get('id')

        # 查找流的 output 端口
        stream_ports = _find_ports(pw_data, stream_node_id, 'out')
        if not stream_ports:
            raise DeviceNotFoundError(f'视频流节点 {stream_node_id} 无输出端口')

        # 查找目标输出的 input 端口
        target_ports = _find_ports(pw_data, target_node_id, 'in')
        if not target_ports:
            raise DeviceNotFoundError(f'视频输出 {target_output_name} 无输入端口')

        # 记录旧链接的端口对以便回滚
        old_link_ports = []
        existing_links = _find_links_for_node(pw_data, stream_node_id)
        for link_obj in existing_links:
            lid = link_obj.get('id')
            if lid is not None:
                link_info = link_obj.get('info', {})
                old_link_ports.append((link_info.get('output-port-id'), link_info.get('input-port-id')))
                run_command(f"pw-cli unlink {lid}", timeout=5)

        # 创建新链接（视频端口按顺序匹配）
        created_links = []
        try:
            for i, sp in enumerate(stream_ports):
                if i < len(target_ports):
                    tp = target_ports[i]
                else:
                    tp = target_ports[0]

                link_result = run_command(
                    f"pw-cli link {sp['id']} {tp['id']}",
                    timeout=5
                )
                if link_result['success']:
                    created_links.append({
                        'output_port': sp['id'],
                        'input_port': tp['id'],
                    })
                    logger.info(f"创建视频链接: port {sp['id']} -> port {tp['id']}")
                else:
                    raise CommandError(f"创建视频链接失败: {link_result.get('stderr', '未知错误')}")

            if not created_links:
                raise CommandError('未能创建任何视频链接')
        except CommandError:
            # 回滚：尝试恢复旧链接
            for out_port, in_port in old_link_ports:
                if out_port is not None and in_port is not None:
                    run_command(f"pw-cli link {out_port} {in_port}", timeout=5)
            raise

        return {
            'stream_node_id': stream_node_id,
            'target_output_name': target_output_name,
            'target_node_id': target_node_id,
            'links_created': created_links,
        }

    except MediaBridgeError:
        raise
    except Exception as e:
        logger.error(f"路由视频流失败: {e}")
        raise CommandError(str(e)) from e


def get_all_links():
    """查询所有 PipeWire 链接"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        links = []
        for link_obj in _find_pw_links(pw_data):
            links.append(_build_link_info(link_obj, pw_data))

        return links

    except MediaBridgeError:
        raise
    except Exception as e:
        logger.error(f"获取所有链接失败: {e}")
        raise CommandError(str(e)) from e
