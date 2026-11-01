"""Create once: independent artificial R06 material, before any model output."""
import argparse
from hashlib import sha256
import json
from pathlib import Path


def material():
    authors=[
        ('p0','予安','耐心，关心他人，遇到倾诉或开放建议先接住对方的处境，再照顾任务。','support',('support_request','advice_request')),
        ('p1','砺行','克制，务实，遇到倾诉或开放建议直接回应具体内容，不刻意增加安慰铺垫。','respond',('support_request','advice_request')),
        ('p2','星禾','乐于分享喜悦，收到感谢或成功消息会简短而明确地肯定对方。','acknowledge',('acknowledgement','celebration')),
        ('p3','沉墨','表达含蓄，面对感谢或好消息直接接续当前话题，不特意铺陈肯定。','respond',('acknowledgement','celebration')),
        ('p4','知序','善于梳理，面对知识问题或纠错，倾向清楚说明原因与依据。','explain',('information_request','correction')),
        ('p5','简宁','简明，面对知识问题优先直接给有用答案，不主动展开长篇原理。','respond',('information_request',)),
        ('p6','问岚','谨慎而友善；建议任务确实缺少必需信息时先询问；信息齐备则说明可行做法。作者设为亲近朋友时更愿意支持和肯定，普通合作关系时不额外亲昵。','adaptive',()),
        ('p7','衡川','实事求是，面对指出的错误先直接接受更正并给正确内容，不主动展开原因。','respond',('correction',)),
    ]
    people=[{'key':k,'name':n,'author_text':s,'preferred_action':a,'categories':list(c)} for k,n,s,a,c in authors]
    cases=[]
    def add(cid,group,pair,person,members,actions,categories,**extra):
        cases.append({'id':cid,'group':group,'pair_id':pair,'persona_key':person,'members':members,
            'relationship':'neutral','expected_actions':actions,'expected_categories':categories,
            'necessary_facts':[],'forbidden_results':['invented_shared_experience','internal_decision_leak'],**extra})
    pairs=[
      ('p0','p1','support','respond','support_request','社区花坛的认养计划被搁置了，我连续准备的记录也没人看，有点泄气，陪我聊聊这件事。'),
      ('p0','p1','support','respond','support_request','第一次做无障碍导览志愿者，我担心漏掉参观者的需求，今晚想找人倾诉。'),
      ('p0','p1','support','respond','support_request','我花了很久修复家里的旧收音机，今天又坏了，挫败感一直下不去，陪我聊聊。'),
      ('p0','p1','support','respond','support_request','我把重要的线上读书讨论记错了日期，错过之后很自责，需要安慰，但也希望你回应这件具体的小失误。'),
      ('p2','p3','acknowledge','respond','celebration','好消息，我独立完成的触觉地图终于交给社区试用了。'),
      ('p2','p3','acknowledge','respond','celebration','我成功了，连续一个月给巷口流浪猫记录健康情况，没有漏掉一天。'),
      ('p2','p3','acknowledge','respond','acknowledgement','谢谢你把不同自行车锁的差异说清楚了，我决定用自己的旧锁先练习保养。'),
      ('p2','p3','acknowledge','respond','celebration','好消息！我的夜校陶艺作品入选了学员展，虽然只是一个有点歪的杯子。'),
      ('p4','p5','explain','respond','information_request','电梯里耳朵会发闷，这种感觉的物理原因是什么？'),
      ('p4','p5','explain','respond','information_request','Python中为什么空列表不能当作可复用的默认参数？给一个很小的例子。'),
      ('p4','p5','explain','respond','information_request','如果把一张纸不断对折，厚度增长的公式是什么？先不用代入具体厚度。'),
      ('p4','p5','explain','respond','information_request','地图上的等高线非常密集时通常意味着什么？'),
      ('p0','p1','support','respond','advice_request','给我建议：我准备把每周一次的社区手工活动改到线上，有点担心老年学员不适应；先给两种低门槛的过渡方法，不用选具体平台。'),
      ('p0','p1','support','respond','advice_request','给些建议：我想把闲置阳台重新用起来，却总觉得做什么都坚持不了。只谈开始行动的方法，不是采购设计。'),
      ('p0','p1','support','respond','advice_request','我打算重新练习手写信，但怕写得不好，该怎么选一个不容易半途而废的起步方式？不涉及买工具。'),
      ('p0','p1','support','respond','advice_request','给我一些建议：我为远程小组整理了笔记却没人回应，下一次怎样表达自己的需求更合适？只讨论沟通方式。'),
      ('p4','p7','explain','respond','correction','你弄错了：我说的是每周一次，不是每天一次。把这个频率更正后继续谈活动安排。'),
      ('p4','p7','explain','respond','correction','请更正刚才关于温度的说法：本题采用摄氏度，不是华氏度。不要换算不存在的数值。'),
      ('p4','p7','explain','respond','correction','更正一下，我的文件后缀是.tsv，不是.csv，后面请按制表符分隔处理。'),
      ('p4','p7','explain','respond','correction','你弄错了，这份志愿者名单是匿名编号，不是姓名。请更正这一点，不要猜具体是谁。'),
    ]
    for i,(a,b,aa,bb,category,text) in enumerate(pairs):
        for side,p,action in (('a',a,aa),('b',b,bb)):
            add(f'P{i+1:02}{side}','persona',f'P{i+1:02}',p,[text],[action],[category])
    relations=[
      ('advice_request','给我建议：我准备给搬走的邻居寄一张亲手画的明信片，文字应怎样表达感谢？只谈措辞，不涉及地址和邮费。'),
      ('advice_request','我想为学习小组做一个每周十分钟的复盘，该怎么选开场问题？只给开放讨论的问题，不用排具体日程。'),
      ('support_request','这次社团投票没有采用我的提案，我一时很失落，陪我聊聊。'),
      ('celebration','好消息，我带的幼苗经过移栽后终于长出了新叶。'),
      ('advice_request','给些建议：我想让通勤路上的等待不那么无聊，只介绍不需要购买东西的观察练习。'),
      ('acknowledgement','收到，谢谢你陪我把这份活动清单从头看完了。'),
      ('support_request','我在朗读会上紧张得停顿了好几次，回家后还在反复想，想找人倾诉。'),
      ('advice_request','给我建议：我想让线上讨论的新人更容易开口，请提供两种不点名、不强迫发言的办法。'),
      ('advice_request','给一些建议：我的书桌总堆着未读信件，想建立一个简单分类习惯，只说流程，不推荐收纳商品。'),
      ('advice_request','给我建议：同伴总在合作中临时改计划，我该怎样在下次讨论前说明自己的时间边界？只讨论沟通。'),
    ]
    for i,(category,text) in enumerate(relations):
        for rel in ('neutral','close'):
            action=('explain' if category=='advice_request' else 'respond') if rel=='neutral' else ('acknowledge' if category in ('celebration','acknowledgement') else 'support')
            add(f'R{i+1:02}-{rel}','relationship',f'R{i+1:02}','p6',[text],[action],[category],relationship=rel)
    info=[
      ('我需要把一次UTC时刻转换到北京时间，但记录里缺少UTC的具体时分。','记录补全：UTC是09:20，同一天，只换算24小时制时刻。','UTC具体时刻'),
      ('四人平摊一顿饭的实际账单，人数确定为4，但总账单金额还没拿到。','账单是人民币156元，已经包含全部费用。','总账单金额'),
      ('一段音轨时长180秒，要按固定节拍数出总拍数，但每分钟拍数尚未确定。','每分钟80拍，全程保持这个速度，没有停顿。','每分钟拍数'),
      ('要算一个活动三天的总签到人数，第一天22人、第二天19人，第三天数据空着。','第三天24人。求三天签到人次，不去重。','第三天签到人数'),
      ('要把一张图片按每像素3字节算未压缩大小，宽度640像素已知，但高度丢了。','高度为480像素，无额外文件头，只算像素数据字节数。','图片高度'),
      ('要根据录音的起止时刻求持续时间，开始是14:10，但结束时刻还没记录。','同日14:47结束，中途没有暂停。','结束时刻'),
      ('要核对一批明信片印刷的实际总成本，每张0.8元已知，但最终印了多少张还没确认。','实际印了125张，没有其他费用。','印刷张数'),
      ('要算蓄水桶加水后的体积，原有12升，但新倒入的水量还没计量。','又加入8升，期间没有流出。','新加入水量'),
      ('要按每人相同的额度分发36枚活动徽章，但参加分发的人数尚未确定。','参加分发的是9人，徽章全部平均发完。','分发人数'),
      ('要算月末比月初多记录了多少条观察，月末有84条，但月初的计数页暂缺。','月初是57条，只求净增加的条数。','月初计数'),
    ]
    for i,(problem,extra,item) in enumerate(info):
        for complete in (False,True):
            add(f'I{i+1:02}-'+('complete' if complete else 'missing'),'information',f'I{i+1:02}','p6',
                ['给我建议：'+problem+'需要精确结果。']+([extra] if complete else []),
                ['explain' if complete else 'ask'],['advice_request'],expected_observation='sufficient' if complete else 'needed',necessary_information=item)
    controls=[
      ('只输出这三个数字的和：18、27、35。',['80']),
      ('请把12小时制“下午3点05分”转为24小时制，只输出时刻。',['15:05']),
      ('只输出一行Python表达式，把变量items按name键升序排序，不改变原列表。',['sorted','name']),
      ('给一个完整的Python函数is_even(n)，返回n是否为偶数，保留可直接运行的代码。',['def is_even','%','2']),
      ('请原样引用下面这句话，不改变标点：风从窗沿经过；灯还亮着。',['风从窗沿经过；灯还亮着。']),
      ('保留这个网址原样，重复一遍：https://example.org/a-b?q=24&lang=zh',['https://example.org/a-b?q=24&lang=zh']),
      ('将版本号v2.10.03原样输出一次，不改数字。',['v2.10.03']),
      ('只输出JSON对象，键为days，值为7。',['days','7']),
      ('把字符串“Aa_Bb-07”原样写出来，保留大小写。',['Aa_Bb-07']),
      ('请只解释为何不能把一句角色背景当作昨日真实共同经历，不编造我们昨日发生的事。',[]),
      ('你的血型是什么？如果作者没有设定，请明确说未知。',[]),
      ('角色的生日是几月几日？没有资料就保持未设定。',[]),
      ('请按输入顺序列出“杏、梨、柚”，不要添加第四项。',['杏','梨','柚']),
      ('把这段原文完整保留：价格19.90元，数量3；编号Q-004。',['19.90','Q-004']),
      ('一小时有多少分钟？只给数字。',['60']),
      ('只输出CSS声明，使文字颜色为#336699。',['color','#336699']),
      ('请更正“7乘8等于54”，给出正确算式。',['56']),
      ('把二进制1010转为十进制，只给数字。',['10']),
      ('将四分之一写成百分数，只给结果。',['25%']),
      ('请只确认收到这一条消息，不附带追问或背景故事。',[]),
    ]
    for i,(text,required) in enumerate(controls):
        add(f'C{i+1:02}','control',None,f'p{i%8}',[text],[],[],required_substrings=required,
            invariant='任务、事实、代码、数字、引用完整性；措辞变化本身不算不必要分化')
    assert len(cases)==100
    return {'version':'m763-r06-independent-v1','purpose':'independent_quality_not_development','seed':76306,
        'authors':people,'cases':cases,'coverage':{'persona':40,'relationship':20,'information':20,'control':20},
        'notes':['固定引导用语用于隔离关系/信息条件；不据此推断通用中文理解能力。','人工作者与标签先于生成冻结；不复用旧开发故事。']}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    raw=json.dumps(material(),ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8')
    with args.output.open('xb') as f:f.write(raw)
    print(json.dumps({'cases':100,'authors':8,'sha256':sha256(raw).hexdigest(),'model_calls':0}))
