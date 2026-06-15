import logging
from utils import (run_command, pw_dump, find_pw_node, get_node_id_by_name,
                   get_node_name_by_id, get_prop_with_fallback, find_device_props,
                   _find_pw_links, _get_ports_for_node, _build_link_info)
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaHubError

logger = logging.getLogger('MediaHub')


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


def get_audio_streams():
    """查询所有活跃音频流（Audio/Playback 和 Audio/Record）"""
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
            if media_class not in ('Audio/Playback', 'Audio/Record'):
                continue

            node_id = obj.get('id')
            name = props.get('node.name', '')
            friendly_name = (props.get('node.description', '')
                             or props.get('node.nick', '')
                             or name)
            application = props.get('application.name', '')
            media_role = props.get('media.role', '')

            # 查找该流的所有 Link
            links = _find_links_for_node(pw_data, node_id)
            link_infos = [_build_link_info(l, pw_data) for l in links]

            # 收集连接的 sink 名称
            connected_sinks = []
            for li in link_infos:
                sink_node_id = li.get('input_node_id') if media_class == 'Audio/Playback' else li.get('output_node_id')
                if sink_node_id:
                    sink_name = get_node_name_by_id(sink_node_id)
                    if sink_name and sink_name not in connected_sinks:
                        connected_sinks.append(sink_name)

            # 为每个 link 补充 sink 信息
            stream_links = []
            for li in link_infos:
                if media_class == 'Audio/Playback':
                    sink_node_id = li.get('input_node_id')
                else:
                    sink_node_id = li.get('output_node_id')
                sink_name = get_node_name_by_id(sink_node_id) if sink_node_id else ''
                stream_links.append({
                    'link_id': li['link_id'],
                    'output_port': li['output_port'],
                    'input_port': li['input_port'],
                    'sink_node_id': sink_node_id,
                    'sink_name': sink_name,
                })

            # 获取流音量和静音状态
            ch_vols = info.get('params', {}).get('Props', {})
            if isinstance(ch_vols, dict):
                vol_list = ch_vols.get('channelVolumes', [])
                mute_state = bool(ch_vols.get('mute', False))
            else:
                vol_list = []
                mute_state = False
            vol_percent = 0
            if vol_list and isinstance(vol_list, list):
                valid = [float(cv) for cv in vol_list if isinstance(cv, (int, float))]
                if valid:
                    vol_percent = min(round((sum(valid) / len(valid)) / 65536 * 100), 150)

            streams.append({
                'node_id': node_id,
                'name': name,
                'friendly_name': friendly_name,
                'application': application,
                'media_class': media_class,
                'media_role': media_role,
                'connected_sinks': connected_sinks,
                'links': stream_links,
                'volume': vol_percent,
                'muted': mute_state,
            })

        return streams

    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"获取音频流失败: {e}")
        raise CommandError(str(e)) from e


def route_audio_stream(stream_node_id, target_sink_name):
    """将音频流路由到指定 Sink"""
    try:
        try:
            stream_node_id = int(stream_node_id)
        except (TypeError, ValueError):
            raise InvalidParamError('无效的流ID')

        if not target_sink_name:
            raise InvalidParamError('参数不完整：需要 target_sink_name')

        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        # 查找流节点
        stream_node = find_pw_node(pw_data, node_id=stream_node_id)
        if not stream_node:
            raise DeviceNotFoundError(f'未找到流节点 ID: {stream_node_id}')

        # 查找目标 Sink 节点
        sink_node = find_pw_node(pw_data, name=target_sink_name)
        if not sink_node:
            raise DeviceNotFoundError(f'未找到 Sink: {target_sink_name}')

        sink_node_id = sink_node.get('id')

        # 查找流的 output 端口
        stream_ports = _find_ports(pw_data, stream_node_id, 'out')
        if not stream_ports:
            raise DeviceNotFoundError(f'流节点 {stream_node_id} 无输出端口')

        # 查找 Sink 的 input 端口
        sink_ports = _find_ports(pw_data, sink_node_id, 'in')
        if not sink_ports:
            raise DeviceNotFoundError(f'Sink {target_sink_name} 无输入端口')

        # 断开该流的所有现有链接
        existing_links = _find_links_for_node(pw_data, stream_node_id)
        for link_obj in existing_links:
            link_id = link_obj.get('id')
            if link_id is not None:
                unlink_result = run_command(f"pw-cli unlink {link_id}", timeout=5)
                if unlink_result['success']:
                    logger.info(f"已断开链接: {link_id}")
                else:
                    logger.warning(f"断开链接 {link_id} 失败: {unlink_result.get('stderr', '')}")

        # 按声道匹配创建新链接 (FL→FL, FR→FR 等)
        created_links = []
        sink_port_map = {p['audio_channel']: p for p in sink_ports}

        for sp in stream_ports:
            ch = sp['audio_channel']
            # 优先精确匹配声道，否则使用第一个可用端口
            target_port = sink_port_map.get(ch)
            if not target_port:
                # 尝试匹配 MONO → FL 的回退
                if ch == 'MONO':
                    target_port = sink_port_map.get('FL') or sink_port_map.get('MONO')
                elif ch == 'FL':
                    target_port = sink_port_map.get('MONO') or sink_port_map.get('FL')

            if not target_port:
                # 使用未匹配的 sink 端口
                used_ids = {t['input_port'] for t in created_links} if created_links else set()
                for tp in sink_ports:
                    if tp['id'] not in used_ids:
                        target_port = tp
                        break

            if not target_port:
                logger.warning(f"流端口 {sp['id']} (channel={ch}) 无匹配 Sink 端口")
                continue

            link_result = run_command(
                f"pw-cli link {sp['id']} {target_port['id']}",
                timeout=5
            )
            if link_result['success']:
                created_links.append({
                    'output_port': sp['id'],
                    'input_port': target_port['id'],
                    'channel': ch,
                })
                logger.info(f"创建链接: port {sp['id']} -> port {target_port['id']} (channel={ch})")
            else:
                logger.warning(f"创建链接失败: port {sp['id']} -> port {target_port['id']}: {link_result.get('stderr', '')}")

        if not created_links:
            raise CommandError('未能创建任何链接')

        # 验证链接
        pw_data_verify = pw_dump()
        verify_links = _find_links_for_node(pw_data_verify, stream_node_id)
        verify_link_infos = [_build_link_info(l, pw_data_verify) for l in verify_links]
        connected_to_target = any(
            li.get('input_node_id') == sink_node_id
            for li in verify_link_infos
        )

        return {
            'stream_node_id': stream_node_id,
            'target_sink_name': target_sink_name,
            'sink_node_id': sink_node_id,
            'links_created': created_links,
            'verified': connected_to_target,
        }

    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"路由音频流失败: {e}")
        raise CommandError(str(e)) from e


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
            return {'unlinked': unlinked, 'failed': failed}

        return {'unlinked': unlinked}

    except MediaHubError:
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

    except MediaHubError:
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

        # 断开该流的所有现有链接
        existing_links = _find_links_for_node(pw_data, stream_node_id)
        for link_obj in existing_links:
            lid = link_obj.get('id')
            if lid is not None:
                run_command(f"pw-cli unlink {lid}", timeout=5)

        # 创建新链接（视频端口按顺序匹配）
        created_links = []
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
                logger.warning(f"创建视频链接失败: {link_result.get('stderr', '')}")

        if not created_links:
            raise CommandError('未能创建任何视频链接')

        return {
            'stream_node_id': stream_node_id,
            'target_output_name': target_output_name,
            'target_node_id': target_node_id,
            'links_created': created_links,
        }

    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"路由视频流失败: {e}")
        raise CommandError(str(e)) from e


def route_bluetooth_source(source_name, target_app_name):
    """将蓝牙音频源路由到指定应用的 Record 节点"""
    try:
        if not source_name or not target_app_name:
            raise InvalidParamError('参数不完整：需要 source_name 和 target_app_name')

        pw_data = pw_dump()
        if not pw_data:
            raise CommandError('PipeWire 未运行或无数据')

        # 查找蓝牙 Source 节点
        source_node = find_pw_node(pw_data, name=source_name)
        if not source_node:
            raise DeviceNotFoundError(f'未找到蓝牙音频源: {source_name}')

        source_node_id = source_node.get('id')

        # 查找目标应用的 Record 节点
        target_node = None
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            info = obj.get('info', {})
            props = info.get('props', {})
            media_class = props.get('media.class', '')
            if media_class != 'Audio/Record':
                continue
            app_name = props.get('application.name', '')
            node_name = props.get('node.name', '')
            if app_name == target_app_name or target_app_name in node_name:
                target_node = obj
                break

        if not target_node:
            raise DeviceNotFoundError(f'未找到应用的 Record 节点: {target_app_name}')

        target_node_id = target_node.get('id')

        # 查找 Source 的 output 端口
        source_ports = _find_ports(pw_data, source_node_id, 'out')
        if not source_ports:
            raise DeviceNotFoundError(f'蓝牙源 {source_name} 无输出端口')

        # 查找 Record 节点的 input 端口
        target_ports = _find_ports(pw_data, target_node_id, 'in')
        if not target_ports:
            raise DeviceNotFoundError(f'应用 {target_app_name} Record 节点无输入端口')

        # 断开 Source 的现有链接
        existing_links = _find_links_for_node(pw_data, source_node_id)
        for link_obj in existing_links:
            lid = link_obj.get('id')
            if lid is not None:
                run_command(f"pw-cli unlink {lid}", timeout=5)

        # 按声道匹配创建链接
        created_links = []
        target_port_map = {p['audio_channel']: p for p in target_ports}

        for sp in source_ports:
            ch = sp['audio_channel']
            tp = target_port_map.get(ch)
            if not tp:
                if ch == 'MONO':
                    tp = target_port_map.get('FL') or target_port_map.get('MONO')
                elif ch == 'FL':
                    tp = target_port_map.get('MONO') or target_port_map.get('FL')

            if not tp:
                used_ids = {cl['input_port'] for cl in created_links}
                for candidate in target_ports:
                    if candidate['id'] not in used_ids:
                        tp = candidate
                        break

            if not tp:
                continue

            link_result = run_command(
                f"pw-cli link {sp['id']} {tp['id']}",
                timeout=5
            )
            if link_result['success']:
                created_links.append({
                    'output_port': sp['id'],
                    'input_port': tp['id'],
                    'channel': ch,
                })
                logger.info(f"创建蓝牙路由: port {sp['id']} -> port {tp['id']} (channel={ch})")
            else:
                logger.warning(f"创建蓝牙路由失败: {link_result.get('stderr', '')}")

        if not created_links:
            raise CommandError('未能创建任何蓝牙路由链接')

        return {
            'source_name': source_name,
            'source_node_id': source_node_id,
            'target_app_name': target_app_name,
            'target_node_id': target_node_id,
            'links_created': created_links,
        }

    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"路由蓝牙音频源失败: {e}")
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

    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"获取所有链接失败: {e}")
        raise CommandError(str(e)) from e
