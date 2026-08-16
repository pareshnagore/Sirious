import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:sirious/main.dart';

void main() {
  testWidgets('Sirious app renders start button', (WidgetTester tester) async {
    await tester.pumpWidget(const SiriousApp());

    expect(find.text('Start session'), findsOneWidget);
    expect(find.text('Sirious'), findsOneWidget);
  });
}
