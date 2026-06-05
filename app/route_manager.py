import logging
import shlex
from utils import run_command, pw_dump, find_pw_node, get_node_id_by_name, get_node_name_by_id, get_prop_with_fallback, find_device_props

logger = logging.getLogger('MediaHub')


def _find_ports(pw_data, node_id, direction):
    """查找指定节点的所有端口，返回 [{id, direction, audio_channel, node_id}]"""
    ports = []
    for obj in pw_data:
        if not isinstance(obj, dict):
            continue
        if obj.get('type') != 'PipeWire:Interface:Port':
            continue
        info = obj.get('info', {})
        props = info.get('props', {})
        if props.get('node.id') != node_id:
            continue
        if props.get('port.direction', '') != direction:
            continue
        port_id = obj.get('id')
        audio_channel = props.get('audio.channel', '')
        ports.append({
            'id': port_id,
            'direction': direction,
            'audio_channel': audio_channel,
            'node_id': node_id,
        })
    return ports


def _find_links_for_node(pw_data, node_id):
    """查找与指定节点相关的所有 Link 对象"""
    # 先收集该节点的所有端口 ID
    port_ids = set()
    for obj in pw_data:
        if not isinstance(obj, dict):
            continue
        if obj.get('type') != 'PipeWire:Interface:Port':
            continue
        info = obj.get('info', {})
        props = info.get('props', {})
        if props.get('node.id') == node_id:
            port_ids.add(obj.get('id'))

    links = []
    for obj in pw_data:
        if not isinstance(obj, dict):
            continue
        if obj.get('type') != 'PipeWire:Interface:Link':
            continue
        info = obj.get('info', {})
        props = info.get('props', {})
        output_port = props.get('link.output.node', '')
        input_port = props.get('link.input.node', '')
        # 也检查端口级属性
        output_port_id = props.get('link.output.port', '')
        input_port_id = props.get('link.input.port', '')
        if output_port == node_id or input_port == node_id:
            links.append(obj)
        elif output_port_id in port_ids or input_port_id in port_ids:
            links.append(obj)
    return links


def _build_link_info(link_obj, pw_data):
    """从 Link 对象构建结构化信息"""
    info = link_obj.get('info', {})
    props = info.get('props', {})
    link_id = link_obj.get('id')

    output_port_id = props.get('link.output.port', '')
    input_port_id = props.get('link.input.port', '')
    output_node_id = props.get('link.output.node', '')
    input_node_id = props.get('link.input.node', '')

    output_name = get_node_name_by_id(output_node_id) if output_node_id else ''
    input_name = get_node_name_by_id(input_node_id) if input_node_id else ''

    return {
        'link_id': link_id,
        'output_port': output_port_id,
        'input_port': input_port_id,
        'output_node_id': output_node_id,
        'output_node_name': output_name,
        'input_node_id': input_node_id,
        'input_node_name': input_name,
    }


def get_audio_streams():
    """查询所有活跃音频流（Audio/Playback 和 Audio/Record）"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'data': [], 'error': 'PipeWire 未运行或无数据'}

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

            streams.append({
                'node_id': node_id,
                'name': name,
                'friendly_name': friendly_name,
                'application': application,
                'media_class': media_class,
                'media_role': media_role,
                'connected_sinks': connected_sinks,
                'links': stream_links,
            })

        return {'success': True, 'data': streams}

    except Exception as e:
        logger.error(f"获取音频流失败: {e}")
        return {'success': False, 'data': [], 'error': str(e)}


def route_audio_stream(stream_node_id, target_sink_name):
    """将音频流路由到指定 Sink"""
    try:
        if stream_node_id is None or not target_sink_name:
            return {'success': False, 'error': '参数不完整：需要 stream_node_id 和 target_sink_name'}

        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'error': 'PipeWire 未运行或无数据'}

        # 查找流节点
        stream_node = find_pw_node(pw_data, node_id=stream_node_id)
        if not stream_node:
            return {'success': False, 'error': f'未找到流节点 ID: {stream_node_id}'}

        # 查找目标 Sink 节点
        sink_node = find_pw_node(pw_data, name=target_sink_name)
        if not sink_node:
            return {'success': False, 'error': f'未找到 Sink: {target_sink_name}'}

        sink_node_id = sink_node.get('id')

        # 查找流的 output 端口
        stream_ports = _find_ports(pw_data, stream_node_id, 'out')
        if not stream_ports:
            return {'success': False, 'error': f'流节点 {stream_node_id} 无输出端口'}

        # 查找 Sink 的 input 端口
        sink_ports = _find_ports(pw_data, sink_node_id, 'in')
        if not sink_ports:
            return {'success': False, 'error': f'Sink {target_sink_name} 无输入端口'}

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
                used_ids = {t['id'] for t in created_links} if created_links else set()
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
            return {'success': False, 'error': '未能创建任何链接'}

        # 验证链接
        pw_data_verify = pw_dump()
        verify_links = _find_links_for_node(pw_data_verify, stream_node_id)
        connected_to_target = any(
            l.get('info', {}).get('props', {}).get('link.input.node') == sink_node_id
            for l in verify_links
        )

        return {
            'success': True,
            'data': {
                'stream_node_id': stream_node_id,
                'target_sink_name': target_sink_name,
                'sink_node_id': sink_node_id,
                'links_created': created_links,
                'verified': connected_to_target,
            },
        }

    except Exception as e:
        logger.error(f"路由音频流失败: {e}")
        return {'success': False, 'error': str(e)}


def unlink_stream(stream_node_id, link_id=None):
    """断开流的链接，指定 link_id 断开特定链接，否则断开所有"""
    try:
        if stream_node_id is None:
            return {'success': False, 'error': '参数不完整：需要 stream_node_id'}

        if link_id is not None:
            result = run_command(f"pw-cli unlink {link_id}", timeout=5)
            if result['success']:
                logger.info(f"已断开链接: {link_id}")
                return {'success': True, 'data': {'unlinked': [link_id]}}
            return {'success': False, 'error': f'断开链接 {link_id} 失败: {result.get("stderr", "")}'}

        # 断开该节点的所有链接
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'error': 'PipeWire 未运行或无数据'}

        links = _find_links_for_node(pw_data, stream_node_id)
        if not links:
            return {'success': True, 'data': {'unlinked': [], 'message': '该流无活跃链接'}}

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
            return {
                'success': True,
                'data': {'unlinked': unlinked, 'failed': failed},
                'error': f'部分链接断开失败: {failed}',
            }

        return {'success': True, 'data': {'unlinked': unlinked}}

    except Exception as e:
        logger.error(f"断开流链接失败: {e}")
        return {'success': False, 'error': str(e)}


def get_video_streams():
    """查询所有活跃视频流"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'data': [], 'error': 'PipeWire 未运行或无数据'}

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

        return {'success': True, 'data': streams}

    except Exception as e:
        logger.error(f"获取视频流失败: {e}")
        return {'success': False, 'data': [], 'error': str(e)}


def route_video_stream(stream_node_id, target_output_name):
    """将视频流路由到指定视频输出"""
    try:
        if stream_node_id is None or not target_output_name:
            return {'success': False, 'error': '参数不完整：需要 stream_node_id 和 target_output_name'}

        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'error': 'PipeWire 未运行或无数据'}

        # 查找视频流节点
        stream_node = find_pw_node(pw_data, node_id=stream_node_id)
        if not stream_node:
            return {'success': False, 'error': f'未找到视频流节点 ID: {stream_node_id}'}

        # 查找目标输出节点
        target_node = find_pw_node(pw_data, name=target_output_name)
        if not target_node:
            return {'success': False, 'error': f'未找到视频输出: {target_output_name}'}

        target_node_id = target_node.get('id')

        # 查找流的 output 端口
        stream_ports = _find_ports(pw_data, stream_node_id, 'out')
        if not stream_ports:
            return {'success': False, 'error': f'视频流节点 {stream_node_id} 无输出端口'}

        # 查找目标输出的 input 端口
        target_ports = _find_ports(pw_data, target_node_id, 'in')
        if not target_ports:
            return {'success': False, 'error': f'视频输出 {target_output_name} 无输入端口'}

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
            return {'success': False, 'error': '未能创建任何视频链接'}

        return {
            'success': True,
            'data': {
                'stream_node_id': stream_node_id,
                'target_output_name': target_output_name,
                'target_node_id': target_node_id,
                'links_created': created_links,
            },
        }

    except Exception as e:
        logger.error(f"路由视频流失败: {e}")
        return {'success': False, 'error': str(e)}


def get_bluetooth_audio_sources():
    """查找所有蓝牙音频输入源"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'data': [], 'error': 'PipeWire 未运行或无数据'}

        sources = []
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            info = obj.get('info', {})
            props = info.get('props', {})
            media_class = props.get('media.class', '')
            if media_class not in ('Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = props.get('node.name', '')
            if 'bluez' not in node_name.lower():
                continue

            node_id = obj.get('id')
            friendly_name = (props.get('node.description', '')
                             or props.get('node.nick', '')
                             or node_name)

            # 从节点名提取 MAC 地址 (如 bluez_output.XX_XX_XX_XX_XX_XX)
            mac = ''
            parts = node_name.split('.')
            for part in parts:
                if '_' in part and len(part.replace('_', '')) == 12:
                    mac = part.replace('_', ':').upper()
                    break

            # 查找连接的应用
            links = _find_links_for_node(pw_data, node_id)
            link_infos = [_build_link_info(l, pw_data) for l in links]

            connected_apps = []
            for li in link_infos:
                app_node_id = li.get('input_node_id')
                if app_node_id:
                    app_name = get_node_name_by_id(app_node_id)
                    if app_name and app_name not in connected_apps:
                        connected_apps.append(app_name)

            source_links = []
            for li in link_infos:
                source_links.append({
                    'link_id': li['link_id'],
                    'output_port': li['output_port'],
                    'input_port': li['input_port'],
                    'connected_node_id': li.get('input_node_id') or li.get('output_node_id'),
                    'connected_node_name': li.get('input_node_name') or li.get('output_node_name'),
                })

            sources.append({
                'node_id': node_id,
                'name': node_name,
                'friendly_name': friendly_name,
                'mac': mac,
                'connected_apps': connected_apps,
                'links': source_links,
            })

        return {'success': True, 'data': sources}

    except Exception as e:
        logger.error(f"获取蓝牙音频源失败: {e}")
        return {'success': False, 'data': [], 'error': str(e)}


def route_bluetooth_source(source_name, target_app_name):
    """将蓝牙音频源路由到指定应用的 Record 节点"""
    try:
        if not source_name or not target_app_name:
            return {'success': False, 'error': '参数不完整：需要 source_name 和 target_app_name'}

        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'error': 'PipeWire 未运行或无数据'}

        # 查找蓝牙 Source 节点
        source_node = find_pw_node(pw_data, name=source_name)
        if not source_node:
            return {'success': False, 'error': f'未找到蓝牙音频源: {source_name}'}

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
            return {'success': False, 'error': f'未找到应用的 Record 节点: {target_app_name}'}

        target_node_id = target_node.get('id')

        # 查找 Source 的 output 端口
        source_ports = _find_ports(pw_data, source_node_id, 'out')
        if not source_ports:
            return {'success': False, 'error': f'蓝牙源 {source_name} 无输出端口'}

        # 查找 Record 节点的 input 端口
        target_ports = _find_ports(pw_data, target_node_id, 'in')
        if not target_ports:
            return {'success': False, 'error': f'应用 {target_app_name} Record 节点无输入端口'}

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
            return {'success': False, 'error': '未能创建任何蓝牙路由链接'}

        return {
            'success': True,
            'data': {
                'source_name': source_name,
                'source_node_id': source_node_id,
                'target_app_name': target_app_name,
                'target_node_id': target_node_id,
                'links_created': created_links,
            },
        }

    except Exception as e:
        logger.error(f"路由蓝牙音频源失败: {e}")
        return {'success': False, 'error': str(e)}


def get_all_links():
    """查询所有 PipeWire 链接"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'data': [], 'error': 'PipeWire 未运行或无数据'}

        links = []
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Link':
                continue
            links.append(_build_link_info(obj, pw_data))

        return {'success': True, 'data': links}

    except Exception as e:
        logger.error(f"获取所有链接失败: {e}")
        return {'success': False, 'data': [], 'error': str(e)}


def get_usb_audio_devices():
    """查找所有 USB 音频设备（Sink 和 Source）"""
    try:
        pw_data = pw_dump()
        if not pw_data:
            return {'success': False, 'data': [], 'error': 'PipeWire 未运行或无数据'}

        devices = []
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            info = obj.get('info', {})
            props = info.get('props', {})
            media_class = props.get('media.class', '')
            if media_class not in ('Audio/Sink', 'Audio/Sink/Virtual',
                                   'Audio/Source', 'Audio/Source/Virtual'):
                continue

            # 检查 device.bus
            device_id_prop = props.get('device.id')
            device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}
            bus = get_prop_with_fallback(props, device_props, 'device.bus', '')
            if bus.lower() != 'usb':
                continue

            node_id = obj.get('id')
            name = props.get('node.name', '')
            friendly_name = (props.get('node.description', '')
                             or props.get('node.nick', '')
                             or name)

            role = 'sink' if 'Sink' in media_class else 'source'

            vendor_id = get_prop_with_fallback(props, device_props, 'device.vendor.id', '')
            product_id = get_prop_with_fallback(props, device_props, 'device.product.id', '')
            vendor_name = get_prop_with_fallback(props, device_props, 'device.vendor.name', '')
            product_name = get_prop_with_fallback(props, device_props, 'device.product.name', '')
            api = get_prop_with_fallback(props, device_props, 'device.api', '')
            bus_path = get_prop_with_fallback(props, device_props, 'device.bus-path', '')
            alsa_card = get_prop_with_fallback(props, device_props, 'alsa.card', '')
            alsa_card_name = get_prop_with_fallback(props, device_props, 'alsa.card_name', '')

            devices.append({
                'node_id': node_id,
                'name': name,
                'friendly_name': friendly_name,
                'media_class': media_class,
                'role': role,
                'vendor_id': vendor_id,
                'product_id': product_id,
                'vendor_name': vendor_name,
                'product_name': product_name,
                'device_api': api,
                'bus_path': bus_path,
                'alsa_card': alsa_card,
                'alsa_card_name': alsa_card_name,
            })

        return {'success': True, 'data': devices}

    except Exception as e:
        logger.error(f"获取 USB 音频设备失败: {e}")
        return {'success': False, 'data': [], 'error': str(e)}
